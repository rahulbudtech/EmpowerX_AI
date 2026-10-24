"""
Shared Redis client (broker/cache foundation for Celery later).

No business logic here — same role as database.py for Postgres.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import redis
from redis import Redis

from app.core.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def get_redis_client() -> Redis:
    """
    Process-wide Redis client.

    decode_responses=True keeps string ops simple for app use;
    Celery will open its own connections from REDIS_URL.
    """
    settings = get_settings()
    # protocol=2 (RESP2): compatible with older Redis builds common on Windows;
    # redis-py 8 defaults to RESP3 HELLO which some servers reject.
    return redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        protocol=2,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )


def ping_redis() -> bool:
    """Return True if Redis answers PING; False on any connection/protocol error."""
    try:
        return bool(get_redis_client().ping())
    except redis.RedisError as exc:
        logger.warning("Redis ping failed: %s", exc)
        return False


def reset_redis_client() -> None:
    """Clear cached client (useful in tests / after config reload)."""
    get_redis_client.cache_clear()
