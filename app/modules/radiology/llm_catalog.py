"""
Allowed LLM provider/model pairs for radiology findings settings UI.

Runtime env may still use other model ids for bootstrap; PUT /llm-settings
only accepts options from this catalog.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LlmModelOption:
    provider: str
    model: str
    label: str


# Keep this list explicit — GET /llm-settings returns these as options.
LLM_MODEL_OPTIONS: tuple[LlmModelOption, ...] = (
    LlmModelOption(
        provider="gemini",
        model="gemini-3.6-flash",
        label="Gemini 3.6 Flash",
    ),
    LlmModelOption(
        provider="anthropic",
        model="claude-sonnet-5-5",
        label="Claude Sonnet 5.5",
    ),
)


def is_allowed_model(*, provider: str, model: str) -> bool:
    key = ((provider or "").strip().lower(), (model or "").strip())
    return any(
        (opt.provider, opt.model) == key for opt in LLM_MODEL_OPTIONS
    )
