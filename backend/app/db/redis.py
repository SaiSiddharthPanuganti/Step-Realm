"""Redis client helpers.

Redis is a *derived, best-effort* store in StepRealm: it holds leaderboard
state that Postgres can always recompute. Nothing here may ever raise into a
request path -- callers are expected to treat a failure as "the leaderboard
is stale" and carry on. See app/services/leaderboard_service.py (phase 8).
"""

from __future__ import annotations

import logging
from typing import Any

import redis
from redis.exceptions import RedisError

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_client: redis.Redis | None = None


def get_redis() -> redis.Redis | None:
    """Return a Redis client, or ``None`` if Redis is not configured.

    ``None`` is a supported state, not an error: the game must remain fully
    playable with no leaderboard, it just reports a degraded leaderboard.
    """
    global _client
    if _client is not None:
        return _client

    settings = get_settings()
    if not settings.redis_url:
        return None

    _client = redis.Redis.from_url(
        settings.redis_url,
        socket_timeout=settings.redis_socket_timeout_seconds,
        socket_connect_timeout=settings.redis_socket_timeout_seconds,
        decode_responses=True,
    )
    return _client


def check_redis() -> str:
    """Probe Redis for the health endpoint. Returns a status string, never
    raises."""
    try:
        client = get_redis()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("redis client construction failed", exc_info=True)
        return f"error:{type(exc).__name__}"

    if client is None:
        return "not_configured"

    try:
        client.ping()
    except RedisError as exc:
        logger.debug("redis health probe failed", exc_info=True)
        return f"unreachable:{type(exc).__name__}"
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("unexpected redis probe failure", exc_info=True)
        return f"error:{type(exc).__name__}"
    return "up"


def reset_redis_client() -> None:
    """Drop the cached client. Used by tests and on shutdown."""
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:  # pragma: no cover - best effort
            logger.debug("error closing redis client", exc_info=True)
    _client = None


def ping_safe(client: Any, *args: Any, **kwargs: Any) -> bool:
    """Run a Redis command, swallowing every failure. For non-critical calls."""
    try:
        client.execute_command(*args, **kwargs)
    except Exception:
        logger.warning(
            "redis command failed", extra={"command": args[0] if args else None},
            exc_info=True,
        )
        return False
    return True
