"""Concurrency: concurrent step syncs must not double-credit or lose steps.

This is the mana-system equivalent of the phase-5 territory race, and the same
reasoning applies: the arithmetic is correct in every sequential test and only
wrong under contention.

The failure mode is specific. Without the row lock in
``mana_service.sync_mana``, all N threads read the same
``pending_step_remainder`` and ``daily_mana_earned``, each computes one whole
block from its own delta, and each writes ``mana = 20``. Last writer wins, so
N concurrent 1,000-step syncs would credit 20 mana instead of 20*N. The
``FOR UPDATE`` serialises them so each observes the previous commit.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from app.core.constants import (
    DAILY_WALKING_MANA_CAP,
    MANA_PER_STEP_BLOCK,
    STEPS_PER_MANA_BLOCK,
)
from app.db.session import get_session_factory
from app.services.mana_service import sync_mana


THREADS = 8


def _concurrent_syncs(user_id: int, deltas: list[int]) -> list[BaseException]:
    """Run simultaneous step syncs for one user. Returns any errors."""
    barrier = threading.Barrier(len(deltas))
    errors: list[BaseException] = []
    lock = threading.Lock()

    def attempt(steps: int) -> None:
        with get_session_factory()() as session:
            try:
                barrier.wait(timeout=20)
                sync_mana(session, user_id, steps)
            except BaseException as exc:  # noqa: BLE001 - recorded for assertion
                with lock:
                    errors.append(exc)

    with ThreadPoolExecutor(max_workers=len(deltas)) as pool:
        list(pool.map(attempt, deltas))

    return errors


def test_concurrent_full_block_syncs_credit_every_block(
    make_user, user_state, no_step_speed_cap
) -> None:
    """8 simultaneous 1,000-step syncs must credit 8 blocks, not 1.

    Without the row lock every thread reads remainder 0 and writes
    ``mana = 20``; last writer wins and the player is shorted 140 mana.
    """
    user_id = make_user(mana=0)

    errors = _concurrent_syncs(user_id, [STEPS_PER_MANA_BLOCK] * THREADS)
    assert not errors, errors

    state = user_state(user_id)
    assert state["mana"] == THREADS * MANA_PER_STEP_BLOCK
    assert state["daily_mana_earned"] == THREADS * MANA_PER_STEP_BLOCK
    assert state["pending_step_remainder"] == 0


def test_concurrent_partial_syncs_lose_no_steps(
    make_user, user_state, no_step_speed_cap
) -> None:
    """Fragmentary deltas must sum exactly, remainder included."""
    user_id = make_user(mana=0)
    deltas = [500] * THREADS  # 8 x 500 = 4,000 steps = 4 blocks

    errors = _concurrent_syncs(user_id, deltas)
    assert not errors, errors

    state = user_state(user_id)
    assert state["mana"] == 4 * MANA_PER_STEP_BLOCK
    assert state["pending_step_remainder"] == 0


def test_concurrent_syncs_respect_the_daily_cap(
    make_user, user_state, no_step_speed_cap
) -> None:
    """Concurrency must not become a way around the daily limit.

    20 threads x 1,000 steps is 400 mana of walking, but the cap allows 200.
    Serialised, exactly 10 blocks are granted and 10 are forfeited; nothing
    more.
    """
    user_id = make_user(mana=0)
    oversubscribed = THREADS * 3  # 24 syncs, well past the 10-block cap

    errors = _concurrent_syncs(user_id, [STEPS_PER_MANA_BLOCK] * oversubscribed)
    assert not errors, errors

    state = user_state(user_id)
    assert state["daily_mana_earned"] == DAILY_WALKING_MANA_CAP
    assert state["mana"] == DAILY_WALKING_MANA_CAP


def test_concurrent_syncs_for_different_users_do_not_interfere(
    make_user, user_state
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
                sync_mana(session, user_id, STEPS_PER_MANA_BLOCK)
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(attempt, user_ids))

    assert not errors, errors
    for user_id in user_ids:
        assert user_state(user_id)["mana"] == MANA_PER_STEP_BLOCK


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
