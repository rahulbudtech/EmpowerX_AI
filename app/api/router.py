"""
Central API router (wiring only).

Route handlers live under app.api.routes.*. Pathology / dermatology
placeholders are not mounted until those modules are implemented.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import health, radiology

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(radiology.router)
