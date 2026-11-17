"""Shared findings-LLM error types (used by llm.py and provider adapters)."""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.radiology.schemas import LlmTokenUsage, RadiologyReport


class FindingsLLMError(Exception):
    """Raised when the findings model call or JSON parse fails (permanent by default)."""

    def __init__(self, message: str, *, usage: LlmTokenUsage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


class FindingsLLMTransientError(FindingsLLMError):
    """Flaky provider/network — safe for Celery to retry (or try fallback first)."""


@dataclass(slots=True)
class FindingsLlmResult:
    """Typed report plus token usage from one successful LLM call."""

    report: RadiologyReport
    usage: LlmTokenUsage
    provider: str
    model: str


def is_transient_provider_error(exc: BaseException) -> bool:
    """True for timeouts, rate limits, and 5xx-style provider/network failures."""
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

    try:
        import anthropic

        if isinstance(
            exc,
            (
                anthropic.APIConnectionError,
                anthropic.APITimeoutError,
                anthropic.RateLimitError,
                anthropic.InternalServerError,
            ),
        ):
            return True
        if isinstance(exc, anthropic.APIStatusError):
            code = int(getattr(exc, "status_code", 0) or 0)
            if code in {408, 429, 500, 502, 503, 504}:
                return True
    except ImportError:
        pass

    msg = str(exc).lower()
    needles = (
        "timeout",
        "timed out",
        "rate limit",
        "resource exhausted",
        "overloaded",
        "429",
        "unavailable",
        "503",
        "502",
        "504",
        "connection reset",
        "temporarily",
    )
    return any(n in msg for n in needles)


def raise_llm_call_error(
    exc: BaseException,
    *,
    provider: str,
    usage: LlmTokenUsage | None = None,
) -> None:
    """Map a provider exception to transient vs permanent FindingsLLMError."""
    message = f"{provider} API call failed: {exc}"
    if is_transient_provider_error(exc):
        raise FindingsLLMTransientError(message, usage=usage) from exc
    raise FindingsLLMError(message, usage=usage) from exc
