"""Task 03 — fail-closed settings outside local APP_ENV."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(**overrides: object) -> Settings:
    """Build Settings without reading the developer ``.env`` file."""
    base: dict[str, object] = {
        "app_env": "local",
        "database_url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/empowerx",
        "ai_service_key": None,
        "jwt_access_secret": None,
        "jwt_algorithm": "HS256",
        "llm_provider": "gemini",
        "gemini_api_key": None,
        "google_api_key": None,
        "anthropic_api_key": None,
        "llm_fallback_provider": None,
        "enable_openapi": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def test_local_env_allows_default_database_url() -> None:
    cfg = _settings(app_env="local")
    assert cfg.app_env == "local"
    assert "postgres:postgres@" in cfg.database_url


@pytest.mark.parametrize("env", ["staging", "production", "uat"])
def test_deployed_env_rejects_default_database_and_missing_secrets(env: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _settings(app_env=env)

    message = str(exc_info.value)
    assert "DATABASE_URL" in message
    assert "AI_SERVICE_KEY" in message
    assert "JWT_ACCESS_SECRET" in message
    # Must not leak secret material
    assert "change_me" not in message
    assert "postgres:postgres" not in message or "non-default" in message


@pytest.mark.parametrize("env", ["staging", "production", "uat"])
def test_deployed_env_accepts_explicit_secure_settings(env: str) -> None:
    cfg = _settings(
        app_env=env,
        database_url="postgresql+psycopg://app:strong@db:5432/empowerx",
        ai_service_key="service-key-value",
        jwt_access_secret="jwt-secret-value",
        gemini_api_key="gemini-key-value",
    )
    assert cfg.is_deployed_env is True
    assert cfg.ai_service_key == "service-key-value"


@pytest.mark.parametrize("env", ["staging", "production", "uat"])
def test_deployed_env_rejects_non_hs256_algorithm(env: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _settings(
            app_env=env,
            database_url="postgresql+psycopg://app:strong@db:5432/empowerx",
            ai_service_key="service-key-value",
            jwt_access_secret="jwt-secret-value",
            jwt_algorithm="none",
            gemini_api_key="gemini-key-value",
        )
    assert "JWT_ALGORITHM" in str(exc_info.value)
