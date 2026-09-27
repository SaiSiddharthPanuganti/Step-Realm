"""Alembic environment.

Reads the database URL from application settings rather than alembic.ini, so
there is exactly one place that knows where the database is.

Uses the synchronous psycopg driver end to end -- no async_engine here,
which is why app/db/session.py is sync as well.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the `app` package importable when alembic is invoked from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
import app.models  # noqa: E402,F401  (import registers every model)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

_settings = get_settings()


def _database_url() -> str:
    # Alembic wants a plain driver URL. If DATABASE_URL carries the async
    # driver, strip it rather than failing obscurely.
    url = _settings.database_url
    for async_driver in ("+asyncpg", "+psycopg_async", "+asyncmy"):
        if async_driver in url:
            return url.replace(async_driver, "")
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and run migrations against a live database."""
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            # Locks the users/hex_tiles tables during autogenerate on
            # PostgreSQL so a concurrent dev server cannot make autogenerate
            # invent a spurious rename.
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
