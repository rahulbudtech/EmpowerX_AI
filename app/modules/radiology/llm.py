"""
Radiology findings LLM (Gemini primary).

Takes a preprocessed uint8 image + SafeDicomMetadata and returns a typed
RadiologyReport. Fallback provider/model is reserved in config but not
wired yet.

Prompt text lives in prompts/findings.txt and must stay exact.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from app.core.config import Settings, get_settings
from app.modules.radiology.schemas import RadiologyReport, SafeDicomMetadata

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
FINDINGS_PROMPT_PATH = PROMPTS_DIR / "findings.txt"

USER_INSTRUCTION = (
    "Analyze this medical image and return ONLY a JSON object matching the schema.\n"
    "Use the DICOM METADATA block below for technical_metadata (copy values verbatim)."
)


class FindingsLLMError(Exception):
    """Raised when the findings model call or JSON parse fails (permanent by default)."""


class FindingsLLMTransientError(FindingsLLMError):
    """Flaky provider/network — safe for Celery to retry."""


def _is_transient_provider_error(exc: BaseException) -> bool:
    """True for timeouts, rate limits, and 5xx-style Gemini/network failures."""
    if isinstance(exc, (TimeoutError, ConnectionError, BrokenPipeError)):
        return True

    try:
        import httpx

        if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
            return True
    except ImportError:
        pass

    try:
        from google.genai import errors as genai_errors

        if isinstance(exc, genai_errors.APIError):
            code = int(getattr(exc, "code", 0) or 0)
            if code in {408, 429, 500, 502, 503, 504}:
                return True
            server_err = getattr(genai_errors, "ServerError", None)
            if server_err is not None and isinstance(exc, server_err):
                return True
    except ImportError:
        pass

    msg = str(exc).lower()
    needles = (
        "timeout",
        "timed out",
        "rate limit",
        "resource exhausted",
        "429",
        "unavailable",
        "503",
        "502",
        "504",
        "connection reset",
        "temporarily",
    )
    return any(n in msg for n in needles)


def _raise_llm_call_error(exc: BaseException) -> None:
    """Map a provider exception to transient vs permanent FindingsLLMError."""
    message = f"Gemini API call failed: {exc}"
    if _is_transient_provider_error(exc):
        raise FindingsLLMTransientError(message) from exc
    raise FindingsLLMError(message) from exc


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
    """Values the model must copy into technical_metadata (not invent)."""
    return {
        "view_position": derive_view_position(meta),
        "exposure_quality": "Undetermined",
        "quality_notes": None,
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
    """User message: instruction + DICOM METADATA block + extra safe context."""
    dicom_block = build_dicom_metadata_block(meta)
    safe = meta.to_llm_safe_dict()
    parts = [
        USER_INSTRUCTION,
        "DICOM METADATA (copy technical_metadata fields from here):",
        json.dumps(dicom_block, indent=2),
    ]
    if safe:
        parts.append("Additional safe technical context (JSON):")
        parts.append(json.dumps(safe, indent=2))
    return "\n\n".join(parts)


def image_u8_to_pil(image_u8: np.ndarray) -> Image.Image:
    """Convert HxW uint8 array to a PIL grayscale image for Gemini."""
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
# Gemini client
# ---------------------------------------------------------------------------


def _call_gemini(
    *,
    model: str,
    api_key: str,
    system_prompt: str,
    user_text: str,
    image: Image.Image,
) -> str:
    """
    Call Gemini with system instruction + text + image.

    Uses the current Google GenAI SDK (`google-genai`), not the deprecated
    `google-generativeai` package.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    contents = [user_text, image]

    try:
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.1,
                response_mime_type="application/json",
            ),
        )
    except Exception as first_exc:
        if _is_transient_provider_error(first_exc):
            _raise_llm_call_error(first_exc)
        # Some models reject response_mime_type — one local retry without it.
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.1,
                ),
            )
        except Exception as second_exc:
            _raise_llm_call_error(second_exc)

    text = getattr(response, "text", None)
    if not text:
        # Empty payload is often a flaky provider response — allow Celery retry.
        raise FindingsLLMTransientError(f"Gemini returned empty content: {response!r}")
    return text.strip()


def generate_findings_report(
    image_u8: np.ndarray,
    metadata: SafeDicomMetadata,
    *,
    settings: Settings | None = None,
) -> RadiologyReport:
    """
    Run findings LLM on a preprocessed image + safe metadata.

    Primary path: Gemini (LLM_PROVIDER / LLM_MODEL from settings).
    Fallback provider is intentionally not implemented yet.
    """
    cfg = settings or get_settings()
    provider = (cfg.llm_provider or "gemini").strip().lower()

    if provider not in {"gemini", "google"}:
        raise FindingsLLMError(
            f"Unsupported LLM_PROVIDER '{provider}'. Phase 1 supports 'gemini' only "
            "(fallback TBD)."
        )

    api_key = cfg.resolved_gemini_api_key
    if not api_key:
        raise FindingsLLMError(
            "GEMINI_API_KEY (or GOOGLE_API_KEY) is missing. Set it in .env"
        )

    system_prompt = load_system_prompt()
    user_text = build_user_text(metadata)
    image = image_u8_to_pil(image_u8)

    raw = _call_gemini(
        model=cfg.llm_model,
        api_key=api_key,
        system_prompt=system_prompt,
        user_text=user_text,
        image=image,
    )
    return parse_radiology_report(raw)
