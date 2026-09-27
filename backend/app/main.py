"""FastAPI application factory, lifespan, and the root health endpoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.redis import check_redis, reset_redis_client
from app.db.session import check_database, dispose_engine, get_engine

logger = logging.getLogger(__name__)

#: Empty by design. The spec's API surface is unversioned (POST /auth/login,
#: GET /me, POST /territory/claim, ...), so routers mount at the root and the
#: mobile client is written against exactly those paths. If a v2 ever needs
#: to coexist, this is the single place to change.
API_V1_PREFIX = ""


def _schema_state() -> tuple[bool, str | None]:
    """Check that the migrated schema is actually present.

    Worth separating from "is the database up" because the most common
    first-deploy failure is a reachable Postgres with no tables -- Alembic
    simply never ran -- and a plain connectivity probe would report healthy
    while every real endpoint 500s.
    """
    try:
        inspector = inspect(get_engine())
        if not inspector.has_table("users"):
            return False, "missing_tables"
        return True, None
    except SQLAlchemyError as exc:
        logger.debug("schema inspection failed", exc_info=True)
        return False, type(exc).__name__
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("unexpected schema inspection failure", exc_info=True)
        return False, type(exc).__name__


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    settings.validate_for_runtime()

    logger.info(
        "startup environment=%s version=%s",
        settings.environment,
        __version__,
    )

    db_ok, db_error = check_database()
    if not db_ok:
        # Logged, not raised. A container that refuses to start cannot report
        # *why* it is unhealthy, and a database blip should not require a
        # redeploy to recover from.
        logger.error("database unreachable at startup: %s", db_error)
    else:
        schema_ok, schema_error = _schema_state()
        if not schema_ok:
            logger.error(
                "database reachable but schema not ready (%s). "
                "Run: alembic upgrade head",
                schema_error,
            )
        else:
            logger.info("database reachable and schema present")

    yield

    dispose_engine()
    reset_redis_client()
    logger.info("shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Walk & Conquer. Postgres is the single source of truth for all "
            "game state; Redis holds only derived leaderboard state."
        ),
        lifespan=lifespan,
    )

    # GET /world returns ~817 tiles of JSON, which is far too much to push
    # over a mobile connection uncompressed and compresses extremely well
    # (repeated field names, near-identical coordinates). Middleware rather
    # than per-route so every future payload benefits.
    app.add_middleware(GZipMiddleware, minimum_size=500)

    # -- health ------------------------------------------------------------
    @app.get("/health", tags=["ops"], summary="Liveness and dependency probe")
    def health() -> JSONResponse:
        """Report service and dependency health.

        Returns 503 when the database is unreachable or unmigrated so that
        an orchestrator can act on it, and 200 otherwise. Redis is reported
        but never affects the status code: the leaderboard is explicitly
        designed to degrade, so a dead Redis must not take the game down.
        """
        db_ok, db_error = check_database()

        payload: dict[str, Any] = {
            "status": "ok",
            "version": __version__,
            "environment": settings.environment,
            "checks": {"database": "up" if db_ok else "down", "redis": check_redis()},
        }

        if not db_ok:
            payload["status"] = "unhealthy"
            payload["checks"]["database_error"] = db_error
        else:
            schema_ok, schema_error = _schema_state()
            payload["checks"]["schema"] = "present" if schema_ok else "missing"
            if not schema_ok:
                payload["status"] = "degraded"
                payload["checks"]["schema_error"] = schema_error

        return JSONResponse(
            status_code=200 if db_ok else 503, content=payload
        )

    @app.get("/health/live", tags=["ops"], summary="Liveness only")
    def liveness() -> dict[str, str]:
        """Liveness probe that touches no dependency.

        Separate from ``/health`` so an orchestrator does not kill and
        restart a perfectly healthy container just because Postgres blinked.
        """
        return {"status": "alive"}

    # -- api ---------------------------------------------------------------
    # Routers are registered as each phase lands; health is the only one in
    # phase 1.
    _register_routers(app)

    return app


def _register_routers(app: FastAPI) -> None:
    # Import the APIRouter *object* from the module. A bare
    # `from app.api.v1 import router` would bind the submodule of that name
    # instead, and include_router would then fail on the missing
    # _contains_router attribute.
    from app.api.v1.router import router as v1_router

    app.include_router(v1_router, prefix=API_V1_PREFIX)


app = create_app()
