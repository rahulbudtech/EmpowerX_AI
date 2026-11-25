"""
Resolve radiology LLM primary/fallback selection.

Precedence: DB singleton row (if present) → env (``LLM_*``) defaults.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.modules.radiology.llm_catalog import LLM_MODEL_OPTIONS, is_allowed_model
from app.modules.radiology.llm_errors import FindingsLLMError
from app.modules.radiology.providers import normalize_provider
from app.modules.radiology.repository import get_llm_settings_row
from app.modules.radiology.schemas import (
    LlmModelOptionResponse,
    LlmModelRef,
    LlmSettingsResponse,
    LlmSettingsUpdateRequest,
)


class LlmSettingsValidationError(Exception):
    """Invalid primary/fallback selection from the API."""


@dataclass(frozen=True, slots=True)
class ResolvedLlmSelection:
    """Runtime primary + optional fallback used by the findings LLM."""

    primary_provider: str
    primary_model: str
    fallback_provider: str | None
    fallback_model: str | None
    source: str  # "db" | "env"
    updated_at: datetime | None = None


def _catalog_options() -> list[LlmModelOptionResponse]:
    return [
        LlmModelOptionResponse(
            provider=opt.provider,
            model=opt.model,
            label=opt.label,
        )
        for opt in LLM_MODEL_OPTIONS
    ]


def selection_from_env(settings: Settings) -> ResolvedLlmSelection:
    """Build selection purely from env / Settings (bootstrap default)."""
    primary_provider = normalize_provider(settings.llm_provider)
    primary_model = (settings.llm_model or "").strip()
    if not primary_model:
        raise FindingsLLMError(
            "LLM_MODEL is missing. Set it in .env or save LLM settings via API."
        )

    fb_provider_raw = (settings.llm_fallback_provider or "").strip()
    fb_model = (settings.llm_fallback_model or "").strip()
    fallback_provider: str | None = None
    fallback_model: str | None = None
    if fb_provider_raw and fb_model:
        fallback_provider = normalize_provider(fb_provider_raw)
        fallback_model = fb_model
        if (fallback_provider, fallback_model) == (primary_provider, primary_model):
            fallback_provider = None
            fallback_model = None

    return ResolvedLlmSelection(
        primary_provider=primary_provider,
        primary_model=primary_model,
        fallback_provider=fallback_provider,
        fallback_model=fallback_model,
        source="env",
        updated_at=None,
    )


def resolve_llm_selection(
    db: Session | None,
    *,
    settings: Settings,
) -> ResolvedLlmSelection:
    """
    Prefer DB settings when a row exists; otherwise use env.

    ``db=None`` always falls back to env (sync scripts without a session).
    """
    if db is not None:
        row = get_llm_settings_row(db)
        if row is not None:
            primary_provider = normalize_provider(row.primary_provider)
            primary_model = (row.primary_model or "").strip()
            fb_provider_raw = (row.fallback_provider or "").strip()
            fb_model = (row.fallback_model or "").strip()
            fallback_provider: str | None = None
            fallback_model: str | None = None
            if fb_provider_raw and fb_model:
                fallback_provider = normalize_provider(fb_provider_raw)
                fallback_model = fb_model
                if (fallback_provider, fallback_model) == (
                    primary_provider,
                    primary_model,
                ):
                    fallback_provider = None
                    fallback_model = None
            return ResolvedLlmSelection(
                primary_provider=primary_provider,
                primary_model=primary_model,
                fallback_provider=fallback_provider,
                fallback_model=fallback_model,
                source="db",
                updated_at=row.updated_at,
            )

    return selection_from_env(settings)


def validate_settings_update(body: LlmSettingsUpdateRequest) -> tuple[
    str,
    str,
    str | None,
    str | None,
]:
    """
    Normalize + validate a PUT body against the allow-list catalog.

    Returns ``(primary_provider, primary_model, fallback_provider, fallback_model)``.
    """
    try:
        primary_provider = normalize_provider(body.primary.provider)
    except FindingsLLMError as exc:
        raise LlmSettingsValidationError(str(exc)) from exc

    primary_model = (body.primary.model or "").strip()
    if not primary_model:
        raise LlmSettingsValidationError("primary.model is required.")
    if not is_allowed_model(provider=primary_provider, model=primary_model):
        raise LlmSettingsValidationError(
            f"Unsupported primary model '{primary_provider}/{primary_model}'. "
            "Use an option from GET /radiology/llm-settings."
        )

    fallback_provider: str | None = None
    fallback_model: str | None = None
    if body.fallback is not None:
        fb_provider_raw = (body.fallback.provider or "").strip()
        fb_model = (body.fallback.model or "").strip()
        if bool(fb_provider_raw) ^ bool(fb_model):
            raise LlmSettingsValidationError(
                "fallback.provider and fallback.model must both be set, or omit fallback."
            )
        if fb_provider_raw and fb_model:
            try:
                fallback_provider = normalize_provider(fb_provider_raw)
            except FindingsLLMError as exc:
                raise LlmSettingsValidationError(str(exc)) from exc
            fallback_model = fb_model
            if not is_allowed_model(
                provider=fallback_provider, model=fallback_model
            ):
                raise LlmSettingsValidationError(
                    f"Unsupported fallback model '{fallback_provider}/{fallback_model}'. "
                    "Use an option from GET /radiology/llm-settings."
                )
            if (fallback_provider, fallback_model) == (
                primary_provider,
                primary_model,
            ):
                raise LlmSettingsValidationError(
                    "fallback must differ from primary."
                )

    return primary_provider, primary_model, fallback_provider, fallback_model


def to_settings_response(selection: ResolvedLlmSelection) -> LlmSettingsResponse:
    """Map a resolved selection into the API response shape."""
    fallback = None
    if selection.fallback_provider and selection.fallback_model:
        fallback = LlmModelRef(
            provider=selection.fallback_provider,
            model=selection.fallback_model,
        )
    return LlmSettingsResponse(
        primary=LlmModelRef(
            provider=selection.primary_provider,
            model=selection.primary_model,
        ),
        fallback=fallback,
        source=selection.source,
        options=_catalog_options(),
        updated_at=selection.updated_at,
    )
