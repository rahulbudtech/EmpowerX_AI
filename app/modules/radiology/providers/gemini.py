"""Gemini findings provider (google-genai SDK)."""

from __future__ import annotations

from typing import Any

from PIL import Image

from app.modules.radiology.llm_errors import (
    FindingsLLMTransientError,
    raise_llm_call_error,
    is_transient_provider_error,
)
from app.modules.radiology.schemas import LlmTokenUsage

_PROVIDER = "gemini"


def parse_usage_metadata(usage_metadata: Any, *, model: str) -> LlmTokenUsage:
    """Map Gemini ``usage_metadata`` onto ``LlmTokenUsage`` (0s when absent)."""
    if usage_metadata is None:
        return LlmTokenUsage(llm_model=model)

    def _int_field(*names: str) -> int:
        for name in names:
            raw = getattr(usage_metadata, name, None)
            if raw is None and isinstance(usage_metadata, dict):
                raw = usage_metadata.get(name)
            if raw is not None:
                try:
                    return max(0, int(raw))
                except (TypeError, ValueError):
                    return 0
        return 0

    return LlmTokenUsage(
        llm_model=model,
        input_tokens=_int_field("prompt_token_count"),
        output_tokens=_int_field("candidates_token_count"),
        reasoning_tokens=_int_field("thoughts_token_count"),
        cached_input_tokens=_int_field("cached_content_token_count"),
        total_tokens=_int_field("total_token_count"),
    )


def call_findings(
    *,
    model: str,
    api_key: str,
    system_prompt: str,
    user_text: str,
    image: Image.Image,
) -> tuple[str, LlmTokenUsage]:
    """
    Call Gemini with system instruction + text + image.

    Returns ``(response_text, token_usage)``.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    contents = [user_text, image]
    usage = LlmTokenUsage(llm_model=model)

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
        if is_transient_provider_error(first_exc):
            raise_llm_call_error(first_exc, provider=_PROVIDER, usage=usage)
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
            raise_llm_call_error(second_exc, provider=_PROVIDER, usage=usage)

    usage = parse_usage_metadata(
        getattr(response, "usage_metadata", None),
        model=model,
    )
    text = getattr(response, "text", None)
    if not text:
        raise FindingsLLMTransientError(
            f"Gemini returned empty content: {response!r}",
            usage=usage,
        )
    return text.strip(), usage
