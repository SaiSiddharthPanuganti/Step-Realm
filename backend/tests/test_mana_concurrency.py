"""Concurrency: concurrent walk reports must not double-credit or lose mana.

This is the mana-system equivalent of the phase-5 territory race, and the same
reasoning applies: the arithmetic is correct in every sequential test and only
wrong under contention.

The failure mode is specific. Without the row lock in
``walk_service.record_walk``, all N threads read the same ``mana`` and
``daily_mana_earned``, each prices its own metres, and each writes
``mana = 20``. Last writer wins, so N concurrent 20-metre reports would credit
20 mana instead of 20*N. The ``FOR UPDATE`` serialises them so each observes
the previous commit.

Plausibility is stubbed out for these tests (``no_walk_speed_cap``): a
report measured against a microseconds-old baseline is correctly refused, but
that is a per-report rule with its own dedicated tests in
test_walk_progress.py. These tests are about the accounting under contention.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.constants import DAILY_WALKING_MANA_CAP
from app.db.session import get_session_factory
from app.services import walk_service

THREADS = 8


@pytest.fixture
def no_walk_speed_cap(monkeypatch) -> None:
    """Make the plausibility check irrelevant: this test is not about speed."""
    from app.services import walk_service as walk

    monkeypatch.setattr(
        walk,
        "check_report_plausibility",
        lambda user, meters, now=None: None,
    )


def _concurrent_walks(
    user_id: int, metres: list[float]
) -> list[BaseException]:
    """Run simultaneous walk reports for one user. Returns any errors."""
    barrier = threading.Barrier(len(metres))
    errors: list[BaseException] = []
    lock = threading.Lock()

    def attempt(meters: float) -> None:
        with get_session_factory()() as session:
            try:
                barrier.wait(timeout=20)
                walk_service.record_walk(
                    session, user_id=user_id, lat=0.0, lon=0.0, meters=meters
                )
            except BaseException as exc:  # noqa: BLE001 - recorded for assertion
                with lock:
                    errors.append(exc)

    with ThreadPoolExecutor(max_workers=len(metres)) as pool:
        list(pool.map(attempt, metres))

    return errors


def test_concurrent_walks_credit_every_report(
    make_user, user_state, no_walk_speed_cap
) -> None:
    """8 simultaneous 20-metre reports must credit 160 mana, not 20.

    Without the row lock every thread reads ``mana = 0`` and writes
    ``mana = 20``; last writer wins and the player is shorted 140 mana.
    """
    user_id = make_user(mana=0)

    errors = _concurrent_walks(user_id, [20.0] * THREADS)
    assert not errors, errors

    state = user_state(user_id)
    assert state["mana"] == THREADS * 20
    assert state["daily_mana_earned"] == THREADS * 20


def test_concurrent_walks_respect_the_daily_cap(
    make_user, user_state, no_walk_speed_cap
) -> None:
    """Concurrency must not become a way around the daily limit.

    The day is seeded 10 mana from the cap, and 8 concurrent 20-metre reports
    want 160 more. Serialised, exactly the 10 mana of headroom are granted and
    the rest is forfeited -- not one thread's whole claim.
    """
    user_id = make_user(mana=0, daily_mana_earned=DAILY_WALKING_MANA_CAP - 10)

    errors = _concurrent_walks(user_id, [20.0] * THREADS)
    assert not errors, errors

    state = user_state(user_id)
    assert state["daily_mana_earned"] == DAILY_WALKING_MANA_CAP
    # Only the headroom landed; the remaining 150 mana of reported walking
    # vanished rather than leaking into a redeemed buffer.
    assert state["mana"] == 10


def test_concurrent_walks_for_different_users_do_not_interfere(
    make_user, user_state, no_walk_speed_cap
) -> None:
    """The lock is per-row, so unrelated users must still proceed in parallel."""
    user_ids = [make_user(mana=0, email=f"racer{i}@example.com") for i in range(THREADS)]

    barrier = threading.Barrier(THREADS)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def attempt(user_id: int) -> None:
        with get_session_factory()() as session:
            try:
                barrier.wait(timeout=20)
                walk_service.record_walk(
                    session, user_id=user_id, lat=0.0, lon=0.0, meters=20.0
                )
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(attempt, user_ids))

    assert not errors, errors
    for user_id in user_ids:
        assert user_state(user_id)["mana"] == 20


def test_regen_is_not_granted_twice_under_concurrent_reads(make_user, user_state, age_user) -> None:
    """A full hour of regen, read concurrently, is 30 mana -- once.

    ``GET /me`` settles and persists, so two overlapping profile reads could
    otherwise each advance the watermark and grant the same 30 mana.
    """
    from datetime import timedelta

    user_id = make_user(mana=0)
    age_user(user_id, by=timedelta(hours=1))

    from app.services.mana_service import refresh_mana
    from app.models.user import User

    barrier = threading.Barrier(THREADS)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def attempt() -> None:
        with get_session_factory()() as session:
            try:
                user = session.get(User, user_id)
                barrier.wait(timeout=20)
                refresh_mana(session, user)
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(lambda _: attempt(), range(THREADS)))

    assert not errors, errors
    assert user_state(user_id)["mana"] == 30