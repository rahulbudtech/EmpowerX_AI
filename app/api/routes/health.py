"""Health / liveness endpoints (ops only — not product logic)."""

from __future__ import annotations

from fastapi import APIRouter

from app.infrastructure.redis import ping_redis

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """
    Liveness + dependency probe.

    ``status`` is ``ok`` when Redis is up, ``degraded`` when Redis is down
    (API process itself is still alive). Celery wiring comes next.
    """
    redis_ok = ping_redis()
    return {
        "status": "ok" if redis_ok else "degraded",
        "redis": "ok" if redis_ok else "unavailable",
    }
