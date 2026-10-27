"""
FastAPI dependencies (HTTP wiring only).

Auth rules live in ``app.modules.auth``; routes depend on these helpers.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from app.modules.auth.schemas import AccessTokenClaims
from app.modules.auth.service import (
    AuthError,
    decode_access_token,
    require_analyze_access_token,
    verify_service_api_key,
)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_bearer = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_service_api_key(
    api_key: str | None = Depends(_api_key_header),
) -> None:
    """Every protected AI route: valid ``X-API-Key`` matching ``AI_SERVICE_KEY``."""
    try:
        verify_service_api_key(api_key)
    except AuthError as exc:
        raise _unauthorized(exc.detail) from exc


def require_analyze_caller(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    _: None = Depends(require_service_api_key),
) -> AccessTokenClaims:
    """
    ``POST /radiology/dicom/analyze``: service key + user access JWT.

    JWT must be ``technician_access`` or ``facility_access`` with ``facilityId``.
    """
    token = creds.credentials if creds is not None else None
    try:
        return require_analyze_access_token(token)
    except AuthError as exc:
        raise _unauthorized(exc.detail) from exc


def require_service_api_key_optional_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    _: None = Depends(require_service_api_key),
) -> AccessTokenClaims | None:
    """
    Service key required; Bearer JWT optional.

    Used for Node worker polls (``GET /radiology/findings/{id}``): key only.
    If ``Authorization`` is present, verify it.
    """
    if creds is None:
        return None
    try:
        return decode_access_token(creds.credentials)
    except AuthError as exc:
        raise _unauthorized(exc.detail) from exc
