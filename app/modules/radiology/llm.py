"""
Radiology findings LLM orchestration.

Shared prompt/parse live here. Provider clients live under ``providers/``.
Primary + optional fallback are chosen from settings (not hardcoded).
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from app.core.config import Settings, get_settings
from app.modules.radiology.llm_errors import (
    FindingsLLMError,
    FindingsLLMTransientError,
    FindingsLlmResult,
)
from app.modules.radiology.llm_settings import (
    ResolvedLlmSelection,
    selection_from_env,
)
from app.modules.radiology.providers import (
    call_provider,
    normalize_provider,
    resolve_api_key,
)
from app.modules.radiology.exposure_quality import heuristic_to_llm_hint
from app.modules.radiology.schemas import RadiologyReport, SafeDicomMetadata

# Re-export for existing ``from app.modules.radiology.llm import ...`` callers.
__all__ = [
    "FindingsLLMError",
    "FindingsLLMTransientError",
    "FindingsLlmResult",
    "generate_findings_report",
    "load_system_prompt",
    "parse_radiology_report",
]

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
FINDINGS_PROMPT_PATH = PROMPTS_DIR / "findings.txt"

USER_INSTRUCTION = (
    "Analyze this medical image and return ONLY a JSON object matching the schema.\n"
    "For technical_metadata.view_position: copy verbatim from the DICOM METADATA block.\n"
    "For technical_metadata.exposure_quality and quality_notes: judge from the image "
    "(use EXPOSURE QUALITY HINTS only as optional guidance)."
)


# ---------------------------------------------------------------------------
# Prompt + user message builders
# ---------------------------------------------------------------------------


@lru_cache
def load_system_prompt() -> str:
    """Load the exact findings system prompt from disk."""
    if not FINDINGS_PROMPT_PATH.exists():
        raise FindingsLLMError(f"Missing prompt file: {FINDINGS_PROMPT_PATH}")
    text = FINDINGS_PROMPT_PATH.read_text(encoding="utf-8").strip()
    if not text:
        raise FindingsLLMError(f"Prompt file is empty: {FINDINGS_PROMPT_PATH}")
    return text


def derive_view_position(meta: SafeDicomMetadata) -> str:
    """
    Derive PA / AP / LAT / UNKNOWN from safe DICOM text fields.

    Used only to populate the DICOM METADATA block the model must copy —
    not inferred from pixels.
    """
    bits = " ".join(
        str(x or "")
        for x in (
            meta.protocol_name,
            meta.series_description,
            meta.study_description,
            " ".join(meta.image_type or []),
        )
    ).upper()
    if re.search(r"\bLAT(ERAL)?\b", bits):
        return "LAT"
    if re.search(r"\bPA\b|POSTEROANTERIOR", bits):
        return "PA"
    if re.search(r"\bAP\b|ANTEROPOSTERIOR", bits):
        return "AP"
    return "UNKNOWN"


def build_dicom_metadata_block(meta: SafeDicomMetadata) -> dict[str, Any]:
    """
    Context for the findings LLM.

    ``view_position`` must be copied into technical_metadata.
    Exposure grade is judged by the model (hints sent separately).
    """
    return {
        "view_position": derive_view_position(meta),
        "patient_sex": meta.patient_sex,
        "patient_birth_date": meta.patient_birth_date,
        "modality": meta.modality,
        "body_part_examined": meta.body_part_examined,
        "protocol_name": meta.protocol_name,
        "rows": meta.rows,
        "columns": meta.columns,
        "pixel_spacing_mm": meta.pixel_spacing_mm,
        "kvp": meta.kvp,
        "exposure": meta.exposure,
    }


def build_user_text(meta: SafeDicomMetadata) -> str:
    """User message: instruction + DICOM block + exposure hints + safe context."""
    dicom_block = build_dicom_metadata_block(meta)
    safe = meta.to_llm_safe_dict()
    parts = [
        USER_INSTRUCTION,
        "DICOM METADATA (copy view_position into technical_metadata from here):",
        json.dumps(dicom_block, indent=2),
    ]
    hint = heuristic_to_llm_hint(
        meta.preprocessing.exposure_assessment if meta.preprocessing else None
    )
    if hint is not None:
        parts.append("EXPOSURE QUALITY HINTS (optional guidance — judge from the image):")
        parts.append(json.dumps(hint, indent=2))
    if safe:
        parts.append("Additional safe technical context (JSON):")
        parts.append(json.dumps(safe, indent=2))
    return "\n\n".join(parts)


def image_u8_to_pil(image_u8: np.ndarray) -> Image.Image:
    """Convert HxW uint8 array to a PIL grayscale image for the LLM."""
    if image_u8.ndim != 2:
        raise FindingsLLMError(f"Expected 2D uint8 image, got shape {image_u8.shape}")
    return Image.fromarray(image_u8, mode="L")


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _extract_json_object(raw: str) -> dict[str, Any]:
    """Strip markdown fences if present and parse the first JSON object."""
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text, flags=re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise FindingsLLMError("Model response did not contain a JSON object.")

    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise FindingsLLMError(f"Invalid JSON from model: {exc}") from exc

    if not isinstance(data, dict):
        raise FindingsLLMError("Model JSON root must be an object.")
    return data


def parse_radiology_report(raw: str) -> RadiologyReport:
    """Parse + validate model text into RadiologyReport."""
    data = _extract_json_object(raw)
    try:
        return RadiologyReport.model_validate(data)
    except Exception as exc:  # noqa: BLE001 — surface validation clearly
        raise FindingsLLMError(f"Report schema validation failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Orchestration (primary → optional fallback)
# ---------------------------------------------------------------------------


def _attempt_slots(selection: ResolvedLlmSelection) -> list[tuple[str, str]]:
    """
    Build ordered (provider, model) attempts from a resolved selection.

    Primary is always first. Fallback is appended when both provider and model
    are set and differ from primary.
    """
    primary_provider = normalize_provider(selection.primary_provider)
    primary_model = (selection.primary_model or "").strip()
    if not primary_model:
        raise FindingsLLMError(
            "Primary LLM model is missing. Set LLM_MODEL or save LLM settings."
        )

    slots: list[tuple[str, str]] = [(primary_provider, primary_model)]

    fb_provider_raw = (selection.fallback_provider or "").strip()
    fb_model = (selection.fallback_model or "").strip()
    if fb_provider_raw and fb_model:
        fb_provider = normalize_provider(fb_provider_raw)
        if (fb_provider, fb_model) != (primary_provider, primary_model):
            slots.append((fb_provider, fb_model))

    return slots


def _invoke_one(
    *,
    provider: str,
    model: str,
    settings: Settings,
    system_prompt: str,
    user_text: str,
    image: Image.Image,
) -> FindingsLlmResult:
    api_key = resolve_api_key(provider, settings=settings)
    raw, usage = call_provider(
        provider,
        model=model,
        api_key=api_key,
        system_prompt=system_prompt,
        user_text=user_text,
        image=image,
    )
    try:
        report = parse_radiology_report(raw)
    except FindingsLLMError as exc:
        raise FindingsLLMError(str(exc), usage=usage) from exc
    return FindingsLlmResult(
        report=report,
        usage=usage,
        provider=normalize_provider(provider),
        model=model,
    )


def generate_findings_report(
    image_u8: np.ndarray,
    metadata: SafeDicomMetadata,
    *,
    settings: Settings | None = None,
    selection: ResolvedLlmSelection | None = None,
) -> FindingsLlmResult:
    """
    Run findings LLM on a preprocessed image + safe metadata.

    Tries primary first; on failure tries fallback once when configured.
    ``selection`` normally comes from DB (preferred) or env via
    ``resolve_llm_selection``; when omitted, env defaults are used.
    """
    cfg = settings or get_settings()
    resolved = selection or selection_from_env(cfg)
    system_prompt = load_system_prompt()
    user_text = build_user_text(metadata)
    image = image_u8_to_pil(image_u8)

    slots = _attempt_slots(resolved)
    last_exc: FindingsLLMError | None = None

    for index, (provider, model) in enumerate(slots):
        try:
            result = _invoke_one(
                provider=provider,
                model=model,
                settings=cfg,
                system_prompt=system_prompt,
                user_text=user_text,
                image=image,
            )
            if index > 0:
                logger.warning(
                    "Findings LLM succeeded on fallback provider=%s model=%s",
                    provider,
                    model,
                )
            else:
                logger.info(
                    "Findings LLM succeeded provider=%s model=%s",
                    provider,
                    model,
                )
            return result
        except FindingsLLMError as exc:
            last_exc = exc
            is_last = index >= len(slots) - 1
            if is_last:
                raise
            logger.warning(
                "Findings LLM primary failed provider=%s model=%s: %s — trying fallback",
                provider,
                model,
                exc,
            )

    assert last_exc is not None
    raise last_exc
