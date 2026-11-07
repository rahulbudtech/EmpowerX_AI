"""Anthropic (Claude) findings provider."""

from __future__ import annotations

import base64
from io import BytesIO
from typing import Any

from PIL import Image

from app.modules.radiology.llm_errors import (
    FindingsLLMTransientError,
    raise_llm_call_error,
)
from app.modules.radiology.schemas import LlmTokenUsage

_PROVIDER = "anthropic"


def parse_usage(usage: Any, *, model: str) -> LlmTokenUsage:
    """Map Anthropic message ``usage`` onto ``LlmTokenUsage``."""
    if usage is None:
        return LlmTokenUsage(llm_model=model)

    def _int_attr(name: str) -> int:
        raw = getattr(usage, name, None)
        if raw is None and isinstance(usage, dict):
            raw = usage.get(name)
        try:
            return max(0, int(raw or 0))
        except (TypeError, ValueError):
            return 0

    input_tokens = _int_attr("input_tokens")
    output_tokens = _int_attr("output_tokens")
    # cache_read_input_tokens when present (prompt caching)
    cached = _int_attr("cache_read_input_tokens")
    total = input_tokens + output_tokens
    return LlmTokenUsage(
        llm_model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=0,
        cached_input_tokens=cached,
        total_tokens=total,
    )


def _image_to_base64_png(image: Image.Image) -> str:
    buf = BytesIO()
    image.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def call_findings(
    *,
    model: str,
    api_key: str,
    system_prompt: str,
    user_text: str,
    image: Image.Image,
) -> tuple[str, LlmTokenUsage]:
    """
    Call Anthropic Messages API with system + text + image.

    Returns ``(response_text, token_usage)``.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    usage = LlmTokenUsage(llm_model=model)
    b64 = _image_to_base64_png(image)

    try:
        # Note: current anthropic SDK Messages.create has no ``temperature`` kwarg.
        message = client.messages.create(
            model=model,
            max_tokens=8192,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": user_text},
                    ],
                }
            ],
        )
    except Exception as exc:
        raise_llm_call_error(exc, provider=_PROVIDER, usage=usage)

    usage = parse_usage(getattr(message, "usage", None), model=model)

    parts: list[str] = []
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", None)
            if text:
                parts.append(str(text))
    raw = "\n".join(parts).strip()
    if not raw:
        raise FindingsLLMTransientError(
            f"Anthropic returned empty content: {message!r}",
            usage=usage,
        )
    return raw, usage
