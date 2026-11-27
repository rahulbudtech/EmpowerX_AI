"""Task 04 — OpenAPI disabled unless ENABLE_OPENAPI=true."""

from __future__ import annotations

from app.core.config import Settings


def test_openapi_disabled_by_default() -> None:
    cfg = Settings(
        _env_file=None,
        app_env="local",
        enable_openapi=False,
    )
    assert cfg.enable_openapi is False


def test_openapi_can_be_enabled_explicitly() -> None:
    cfg = Settings(
        _env_file=None,
        app_env="local",
        enable_openapi=True,
    )
    assert cfg.enable_openapi is True
