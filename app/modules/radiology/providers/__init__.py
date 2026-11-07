"""
LLM provider adapters for radiology findings.

Callers pass a config-selected provider id (gemini / anthropic); this package
dispatches to the matching client. Primary vs fallback is decided in ``llm.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PIL import Image

from app.modules.radiology.llm_errors import FindingsLLMError
from app.modules.radiology.providers import anthropic as anthropic_provider
from app.modules.radiology.providers import gemini as gemini_provider
from app.modules.radiology.schemas import LlmTokenUsage

ProviderCaller = Callable[..., tuple[str, LlmTokenUsage]]

_PROVIDERS: dict[str, ProviderCaller] = {
    "gemini": gemini_provider.call_findings,
    "google": gemini_provider.call_findings,
    "anthropic": anthropic_provider.call_findings,
    "claude": anthropic_provider.call_findings,
}


def normalize_provider(provider: str) -> str:
    """Map aliases to a canonical provider id."""
    key = (provider or "").strip().lower()
    if key in {"gemini", "google"}:
        return "gemini"
    if key in {"anthropic", "claude"}:
        return "anthropic"
    raise FindingsLLMError(
        f"Unsupported LLM provider '{provider}'. "
        "Supported: gemini (google), anthropic (claude)."
    )


def call_provider(
    provider: str,
    *,
    model: str,
    api_key: str,
    system_prompt: str,
    user_text: str,
    image: Image.Image,
) -> tuple[str, LlmTokenUsage]:
    """Dispatch a findings call to the named provider."""
    canonical = normalize_provider(provider)
    # Resolve via alias table (gemini/google → same caller).
    caller = _PROVIDERS.get((provider or "").strip().lower()) or _PROVIDERS[canonical]
    return caller(
        model=model,
        api_key=api_key,
        system_prompt=system_prompt,
        user_text=user_text,
        image=image,
    )


def resolve_api_key(provider: str, *, settings: Any) -> str:
    """Pick the API key for a provider from settings."""
    canonical = normalize_provider(provider)
    if canonical == "gemini":
        key = settings.resolved_gemini_api_key
        if not key:
            raise FindingsLLMError(
                "GEMINI_API_KEY (or GOOGLE_API_KEY) is missing. Set it in .env"
            )
        return key
    if canonical == "anthropic":
        key = getattr(settings, "anthropic_api_key", None)
        if not key:
            raise FindingsLLMError("ANTHROPIC_API_KEY is missing. Set it in .env")
        return key
    raise FindingsLLMError(f"Unsupported LLM provider '{provider}'.")
