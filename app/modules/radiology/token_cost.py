"""
Estimate USD cost from LLM token usage using human-approved env rates.

Rates are not scraped and not date-switched here — operators update
``GEMINI_*_PRICE_PER_MILLION_USD`` when Google changes pricing.

Gemini bills thinking/reasoning tokens at the **output** rate
(see provider docs: "Output price (including thinking tokens)").
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from app.modules.radiology.schemas import LlmTokenUsage

_MILLION = Decimal("1000000")
_COST_QUANT = Decimal("0.000001")  # Numeric(12, 6)


@dataclass(frozen=True, slots=True)
class TokenCostRates:
    """USD price per 1M tokens (human-approved, typically from env)."""

    input_per_million_usd: Decimal
    output_per_million_usd: Decimal
    cached_input_per_million_usd: Decimal

    @classmethod
    def from_floats(
        cls,
        *,
        input_per_million_usd: float,
        output_per_million_usd: float,
        cached_input_per_million_usd: float,
    ) -> TokenCostRates:
        return cls(
            input_per_million_usd=Decimal(str(input_per_million_usd)),
            output_per_million_usd=Decimal(str(output_per_million_usd)),
            cached_input_per_million_usd=Decimal(str(cached_input_per_million_usd)),
        )


def _tokens_to_cost(token_count: int, rate_per_million: Decimal) -> Decimal:
    if token_count <= 0 or rate_per_million <= 0:
        return Decimal("0")
    return (Decimal(token_count) / _MILLION) * rate_per_million


def estimate_cost_usd(usage: LlmTokenUsage, rates: TokenCostRates) -> Decimal:
    """
    Compute estimated USD for one provider call (or accumulated usage).

    Billable output = ``output_tokens + reasoning_tokens`` (thinking @ output rate).
    Cached tokens use the cached-input rate (storage/hour not modeled in v1).
    """
    billable_output = usage.output_tokens + usage.reasoning_tokens
    raw = (
        _tokens_to_cost(usage.input_tokens, rates.input_per_million_usd)
        + _tokens_to_cost(billable_output, rates.output_per_million_usd)
        + _tokens_to_cost(usage.cached_input_tokens, rates.cached_input_per_million_usd)
    )
    return raw.quantize(_COST_QUANT, rounding=ROUND_HALF_UP)
