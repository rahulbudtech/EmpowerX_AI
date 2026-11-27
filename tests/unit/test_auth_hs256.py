"""Task 02 — JWT decode always pins HS256."""

from __future__ import annotations

import time

import jwt
import pytest

from app.core.config import Settings
from app.modules.auth.service import AuthError, decode_access_token


def _cfg(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "local",
        "jwt_access_secret": "unit-test-jwt-secret-at-least-32-bytes",
        "jwt_algorithm": "HS256",
        "ai_service_key": "unit-test-service-key-value",
        "enable_openapi": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def test_valid_hs256_token_decodes() -> None:
    cfg = _cfg()
    token = jwt.encode(
        {
            "sub": "user-1",
            "type": "technician_access",
            "facilityId": "fac-1",
            "exp": int(time.time()) + 600,
        },
        cfg.jwt_access_secret,
        algorithm="HS256",
    )
    claims = decode_access_token(token, settings=cfg)
    assert claims.user_id == "user-1"
    assert claims.facility_id == "fac-1"


def test_token_signed_with_other_algorithm_is_rejected() -> None:
    cfg = _cfg()
    # HS384 signature must not be accepted even if cfg.jwt_algorithm were drifted.
    token = jwt.encode(
        {
            "sub": "user-1",
            "type": "technician_access",
            "facilityId": "fac-1",
            "exp": int(time.time()) + 600,
        },
        cfg.jwt_access_secret,
        algorithm="HS384",
    )
    with pytest.raises(AuthError):
        decode_access_token(token, settings=_cfg(jwt_algorithm="HS384"))
