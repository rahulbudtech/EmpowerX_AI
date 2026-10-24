"""
Empowerx FastAPI application entrypoint.

Run (venv active):
  uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
  # or: bash run_backend.sh
"""

from __future__ import annotations

from fastapi import FastAPI

from app.api.router import api_router
from app.core.logging import setup_logging

setup_logging()

app = FastAPI(
    title="Empowerx API",
    description="Medical AI backend — radiology-first (DICOM findings).",
    version="0.1.0",
)

app.include_router(api_router)


@app.get("/")
def root() -> dict[str, str]:
    """Tiny root hint; prefer /health and /docs."""
    return {
        "service": "empowerx",
        "docs": "/docs",
        "health": "/health",
        "radiology_dicom_analyze": "POST /radiology/dicom/analyze (1–3 files, 202 queued)",
        "radiology_findings": "GET /radiology/findings/{id} (poll status)",
    }
