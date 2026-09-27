"""Shared pytest fixtures.

Two decisions worth knowing about:

1. **The environment is configured at import time, before any app import.**
   ``app.core.config.get_settings`` is lru_cached and ``app.db.session``
   builds its engine lazily from that cached settings object, so anything
   that imports those modules first would pin the dev database into the test
   process. conftest is imported before test modules, which makes this the
   only reliable place to do it.

2. **The schema is created by running Alembic, not ``metadata.create_all``.**
   Using create_all would be faster but would mean the tests exercise the
   models while production runs the migrations -- so a broken migration
   could sit undetected until deploy. Running the real migrations means
   every test run also verifies migration 0001 is applicable.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# --- environment, before any app import ------------------------------------
os.environ.setdefault("JWT_SECRET", "test-secret-" + ("x" * 48))
os.environ["ENVIRONMENT"] = "test"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/steprealm_test",
)
os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
# bcrypt cost 12 is ~250 ms per hash. The suite creates dozens of accounts,
# so drop it to the minimum; correctness of the *scheme* is unaffected.
os.environ["BCRYPT_ROUNDS"] = "4"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

VALID_EMAIL = "walker@example.com"
VALID_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(scope="session", autouse=True)
def _migrated_database() -> Iterator[None]:
    """Rebuild the test schema from scratch by running the real migrations."""
    from alembic import command
    from alembic.config import Config

    from app.db.session import dispose_engine, get_engine

    engine = get_engine()

    # Start from nothing so a stale schema can never make a migration a no-op
    # and hide a failure. Safe because this is the dedicated test database.
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")

    yield

    dispose_engine()


@pytest.fixture(autouse=True)
def _clean_tables() -> Iterator[None]:
    """Empty every table before each test.

    CASCADE covers the FKs between hex_tiles, users and colleges; RESTART
    IDENTITY makes ids predictable across tests.

    ``rate_limit_counters`` is in the list and must stay there. It has no FK to
    cascade from, so leaving it out does not corrupt a test -- it makes every
    test in the suite share one throttling budget, because the test client
    presents the same address to every request. The first test to sign up then
    spends the allowance for every test after it, and the failures look like
    flaky 429s rather than like a missing fixture.
    """
    from app.db.session import get_session_factory

    with get_session_factory()() as session:
        session.execute(
            text(
                "TRUNCATE hex_tiles, users, colleges, rate_limit_counters "
                "RESTART IDENTITY CASCADE"
            )
        )
        session.commit()

    yield


@pytest.fixture
def settings_override(monkeypatch) -> callable:
    """Override ``Settings`` for one test.

    Sets environment variables and clears the ``lru_cache`` on
    ``get_settings``, which is the only supported way to change configuration
    after import: every module holds a reference to the *function*, not to a
    settings object, so clearing the cache reaches all of them. This is also why
    rate-limit ceilings are read per request rather than captured at import --
    with an import-time read, nothing a test does afterwards could change them.

    Restores the cache afterwards so one test's override cannot leak into the
    next.
    """
    from app.core.config import get_settings

    def _override(**values) -> None:
        for key, value in values.items():
            name = key.upper()
            # None means "no such setting", which is not the same as an empty
            # string. Optional settings are declared ``int | None``, and an
            # empty env var fails to parse as an integer rather than meaning
            # "unset" -- so deleting the variable is the only correct encoding.
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, str(value))
        get_settings.cache_clear()

    yield _override

    get_settings.cache_clear()


@pytest.fixture
def client() -> Iterator:
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def session() -> Iterator:
    """A session for direct service/database assertions in tests."""
    from app.db.session import get_session_factory

    with get_session_factory()() as test_session:
        yield test_session


@pytest.fixture
def authed(client) -> callable:
    """Authorization headers for a registered account.

    Registers once per email and reuses the token, so several requests in one
    test share an identity -- which is also what a real client does across an
    app session.
    """
    cache: dict[str, dict[str, str]] = {}

    def _authed(
        email: str = VALID_EMAIL, password: str = VALID_PASSWORD
    ) -> dict[str, str]:
        if email not in cache:
            response = client.post(
                "/auth/signup", json={"email": email, "password": password}
            )
            assert response.status_code == 201, response.text
            cache[email] = {
                "Authorization": f"Bearer {response.json()['access_token']}"
            }
        return cache[email]

    return _authed


@pytest.fixture
def register(client) -> callable:
    """Register an account over HTTP and return (token, user_body)."""

    def _register(email: str = VALID_EMAIL, password: str = VALID_PASSWORD) -> dict:
        response = client.post(
            "/auth/signup", json={"email": email, "password": password}
        )
        assert response.status_code == 201, response.text
        return response.json()

    return _register


@pytest.fixture
def auth_headers(register) -> callable:
    """Build an ``Authorization`` header for a registered account."""

    def _auth_headers(
        email: str = VALID_EMAIL, password: str = VALID_PASSWORD
    ) -> dict[str, str]:
        body = register(email, password)
        return {"Authorization": f"Bearer {body['access_token']}"}

    return _auth_headers


# ---------------------------------------------------------------------------
# Mana-state control
# ---------------------------------------------------------------------------
# Mana tests need to place the clock at an exact instant. The service takes an
# explicit ``now`` for that, but HTTP tests cannot pass one, so they age a row
# directly and let the endpoint compute against the real clock.


def _user_state(user_id: int) -> dict:
    """Read a user's mana columns through a *fresh* session.

    Returns a plain dict on purpose. Handing back an ORM object would pin it to
    a session whose identity map goes stale the moment a service call commits
    elsewhere, and the resulting test failures would look like product bugs.
    """
    from sqlalchemy import text as sql_text

    from app.db.session import get_session_factory

    with get_session_factory()() as read_session:
        row = read_session.execute(
            sql_text(
                "SELECT mana, last_mana_regen_at, daily_mana_earned, "
                "daily_mana_reset_at, pending_step_remainder, "
                "created_at, last_step_sync_at "
                "FROM users WHERE id = :id"
            ),
            {"id": user_id},
        ).mappings().one()
    return dict(row)


@pytest.fixture
def make_user() -> callable:
    """Create a user row with arbitrary mana state. Returns the user id."""
    from app.core.security import hash_password
    from app.db.session import get_session_factory
    from app.models.user import User
    from app.services import mana_service

    def _make(email: str = VALID_EMAIL, **overrides) -> int:
        from app.services import display_name as dn

        now = mana_service.utcnow()
        data: dict = {
            "email": email,
            "password_hash": hash_password(VALID_PASSWORD),
            # Derived the same way production does, so tests exercise the real
            # rule rather than a hand-written name. The retry mirrors signup's:
            # a test that creates "alice@x.com" and "alice@y.com" in one
            # function must not fail on the name they happen to share.
            "display_name": dn.stem_from_email(email),
            "mana": 0,
            "last_mana_regen_at": now,
            "daily_mana_earned": 0,
            "daily_mana_reset_at": mana_service.next_daily_reset(now),
            "pending_step_remainder": 0,
        }
        data.update(overrides)
        with get_session_factory()() as write_session:
            for attempt in range(4):
                user = User(**data)
                write_session.add(user)
                try:
                    write_session.commit()
                except IntegrityError:
                    write_session.rollback()
                    # Only the auto-derived name can collide; an explicit
                    # override that collides should fail the test loudly.
                    if "display_name" in overrides:
                        raise
                    data["display_name"] = dn.with_suffix(data["display_name"])
                    continue
                return user.id
        raise AssertionError("could not allocate a display name in make_user")

    return _make


@pytest.fixture
def user_state() -> callable:
    """Snapshot of a user's mana columns."""
    return _user_state


@pytest.fixture
def age_user() -> callable:
    """Backdate a user's regen watermark by a timedelta.

    Lets an HTTP test simulate time passing without sleeping.
    """
    from datetime import timedelta

    from sqlalchemy import text as sql_text

    from app.db.session import get_session_factory

    def _age(user_id: int, *, by: timedelta) -> None:
        with get_session_factory()() as write_session:
            write_session.execute(
                sql_text(
                    "UPDATE users SET last_mana_regen_at = last_mana_regen_at - "
                    "(:interval)::interval WHERE id = :id"
                ),
                {"interval": f"{int(by.total_seconds())} seconds", "id": user_id},
            )
            write_session.commit()

    return _age


@pytest.fixture
def age_step_sync() -> callable:
    """Backdate a user's step-sync baseline by a timedelta.

    The phase 6 walking-speed cap measures a step delta against the time since
    the last *credited* sync, so any test that submits steps more than once has
    to place that baseline in the past or the second submission is correctly
    refused as implausible. Tests about mana accounting rather than about the cap
    use this to get past it; the cap's own tests deliberately do not.
    """
    from datetime import timedelta

    from sqlalchemy import text as sql_text

    from app.db.session import get_session_factory

    def _age(user_id: int, *, by: timedelta) -> None:
        with get_session_factory()() as write_session:
            write_session.execute(
                sql_text(
                    "UPDATE users SET last_step_sync_at = last_step_sync_at - "
                    "(:interval)::interval WHERE id = :id"
                ),
                {"interval": f"{int(by.total_seconds())} seconds", "id": user_id},
            )
            write_session.commit()

    return _age


@pytest.fixture
def set_step_sync_baseline() -> callable:
    """Place a user's step-sync baseline at an exact instant.

    For the tests that need a precise allowance rather than a plausible one, so
    the boundary between accepted and refused is exact instead of approximate.
    """
    from datetime import datetime

    from sqlalchemy import text as sql_text

    from app.db.session import get_session_factory

    def _set(user_id: int, when: datetime) -> None:
        with get_session_factory()() as write_session:
            write_session.execute(
                sql_text("UPDATE users SET last_step_sync_at = :when WHERE id = :id"),
                {"when": when, "id": user_id},
            )
            write_session.commit()

    return _set


@pytest.fixture
def no_step_speed_cap(monkeypatch) -> None:
    """Make the walking-speed allowance irrelevant to a test.

    Several pre-phase-6 mana tests exist to pin down *accounting* -- remainder
    carry-forward, partial blocks, concurrent credit -- and they submit a whole
    block's worth of steps because that is the size of a block. With the speed
    cap live, a second submission in the same instant is legitimately refused,
    so those tests would measure the cap instead of the accounting they were
    written for, and the tempting fix would be to weaken the cap to suit them.

    This stubs ``step_allowance`` rather than the ``max_steps_per_second``
    setting. The setting is capped at 50 steps/second in ``Settings`` on purpose,
    which is the right guard for production, and 50 is still not enough to make a
    250,000-step block plausible inside a test. Stubbing the function states the
    intent directly -- "this test is not about walking speed" -- while the cap
    keeps its own dedicated tests against the real calculation.
    """
    from app.services import mana_service

    monkeypatch.setattr(
        mana_service, "step_allowance", lambda user, now: 10**9
    )
