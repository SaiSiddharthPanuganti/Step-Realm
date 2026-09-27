"""Engine, session factory, and the FastAPI database dependency.

The engine is created lazily. ``app.core.config.get_settings()`` raises when
``JWT_SECRET`` is absent, and constructing the engine at import time would
turn a misconfigured deployment into an unbootable container -- meaning
``/health`` could never report *why* it is unhealthy. With a lazy engine the
app starts, and the health check can say "database unreachable" or
"configuration invalid" as useful diagnostics.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _build_engine() -> Engine:
    settings = get_settings()
    new_engine = create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=settings.db_pool_pre_ping,
        echo=settings.db_echo,
        # libpq honours this for establishing the TCP connection. Without it
        # an unreachable database hangs the request indefinitely instead of
        # failing, which is fatal for a health check.
        connect_args={"connect_timeout": settings.db_connect_timeout_seconds},
        future=True,
    )

    @event.listens_for(new_engine, "connect")
    def _configure_connection(dbapi_connection, connection_record) -> None:
        """Per-connection session setup: UTC and a statement ceiling.

        All mana arithmetic is a difference between two stored instants, so a
        server running in local time would silently reinterpret them. Doing
        this on connect is the only portable option -- Railway, the Docker
        image, and a developer's laptop all disagree by default.

        Alembic builds its own engine, so migrations are unaffected by the
        statement timeout (a long migration would otherwise fail).
        """
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET TIME ZONE 'UTC'")
            cursor.execute(f"SET statement_timeout = {int(settings.db_statement_timeout_ms)}")
        finally:
            cursor.close()

    return new_engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            # expire_on_commit=False keeps attributes readable after commit()
            # without an implicit refresh SELECT. The claim path returns the
            # player's mana after committing, and that re-SELECT would open a
            # fresh snapshot which, under READ COMMITTED, no longer
            # necessarily reflects the row we just committed.
            expire_on_commit=False,
        )
    return _session_factory


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session.

    This deliberately does *not* commit. Commit and rollback belong to the
    caller: the claim transaction in ``territory_service`` must control its
    own boundary, and a dependency that auto-committed would make it
    impossible to hold row locks across validation and the write.
    """
    session = get_session_factory()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_database() -> tuple[bool, str | None]:
    """Probe the database for the health endpoint.

    Never raises: an unreachable database is a reportable state, not an
    exception, because a health check that 500s tells an operator nothing.
    """
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        # The message can contain the connection string, so log the exception
        # type and keep the detail at DEBUG rather than embedding it here.
        logger.debug("database health probe failed", exc_info=True)
        return False, type(exc).__name__
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("unexpected database health probe failure", exc_info=True)
        return False, type(exc).__name__
    return True, None


def dispose_engine() -> None:
    """Close pooled connections. Used on shutdown and between test modules."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
