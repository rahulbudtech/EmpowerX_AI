"""
Verify EmpowerX Node credentials (no login — verify only).

- ``AI_SERVICE_KEY`` via ``X-API-Key`` (every protected call)
- Access JWT via ``Authorization: Bearer`` (analyze; optional elsewhere)
"""

from __future__ import annotations

import hmac
import logging
from typing import Collection

import jwt
from jwt.exceptions import InvalidTokenError

from app.core.config import Settings, get_settings
from app.modules.auth.constants import (
    ALL_ACCESS_TOKEN_TYPES,
    ANALYZE_ALLOWED_TOKEN_TYPES,
)
from app.modules.auth.schemas import AccessTokenClaims

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """Credential rejected (maps to HTTP 401)."""

    def __init__(self, detail: str = "Unauthorized") -> None:
        self.detail = detail
        super().__init__(detail)


def verify_service_api_key(
    api_key: str | None,
    *,
    settings: Settings | None = None,
) -> None:
    """Reject missing/wrong ``X-API-Key`` (constant-time compare)."""
    cfg = settings or get_settings()
    expected = cfg.ai_service_key
    if not expected:
        logger.error("AI_SERVICE_KEY is not configured")
        raise AuthError("Service authentication is not configured")
    if not api_key or not hmac.compare_digest(api_key, expected):
        raise AuthError("Invalid or missing API key")


def decode_access_token(
    token: str,
    *,
    settings: Settings | None = None,
    allowed_types: Collection[str] | None = None,
) -> AccessTokenClaims:
    """
    Verify HS256 access JWT (signature + ``exp``) and parse claims.

    ``allowed_types`` restricts the ``type`` claim when provided.
    """
    cfg = settings or get_settings()
    secret = cfg.jwt_access_secret
    if not secret:
        logger.error("JWT_ACCESS_SECRET is not configured")
        raise AuthError("Token authentication is not configured")

    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[cfg.jwt_algorithm],
            options={"require": ["exp", "sub", "type"]},
        )
    except InvalidTokenError as exc:
        logger.info("Access token rejected: %s", exc)
        raise AuthError("Invalid or expired access token") from exc

    try:
        claims = AccessTokenClaims.model_validate(payload)
    except Exception as exc:
        raise AuthError("Invalid access token claims") from exc

    types = frozenset(allowed_types) if allowed_types is not None else ALL_ACCESS_TOKEN_TYPES
    if claims.type not in types:
        raise AuthError("Access token type is not allowed")

    return claims


def require_analyze_access_token(
    token: str | None,
    *,
    settings: Settings | None = None,
) -> AccessTokenClaims:
    """
    User JWT required for DICOM analyze.

    Allowed types: ``technician_access``, ``facility_access``.
    Tenant identity comes from ``facilityId`` on the token.
    """
    if not token:
        raise AuthError("Authorization Bearer access token is required")

    claims = decode_access_token(
        token,
        settings=settings,
        allowed_types=ANALYZE_ALLOWED_TOKEN_TYPES,
    )
    if not claims.facility_id:
        raise AuthError("Access token missing facilityId")
    return claims
