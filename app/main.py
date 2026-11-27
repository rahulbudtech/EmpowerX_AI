"""
Empowerx FastAPI application entrypoint.

Run (venv active):
  uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
  # or: bash run_backend.sh
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import get_settings
from app.core.logging import setup_logging

setup_logging()

_settings = get_settings()
_docs = "/docs" if _settings.enable_openapi else None
_redoc = "/redoc" if _settings.enable_openapi else None
_openapi = "/openapi.json" if _settings.enable_openapi else None

app = FastAPI(
    title="Empowerx API",
    description="Medical AI backend — radiology-first (DICOM findings).",
    version="0.1.0",
    docs_url=_docs,
    redoc_url=_redoc,
    openapi_url=_openapi,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/")
def root() -> dict[str, str]:
    """Tiny root hint; prefer /health (and /docs only when explicitly enabled)."""
    payload: dict[str, str] = {
        "service": "empowerx",
        "health": "/health",
        "radiology_dicom_analyze": "POST /radiology/dicom/analyze (1–3 files, 202 queued)",
        "radiology_findings": "GET /radiology/findings/{id} (poll status)",
    }
    if _settings.enable_openapi:
        payload["docs"] = "/docs"
    return payload
