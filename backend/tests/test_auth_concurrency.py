"""Concurrency: exactly one signup may win a given email.

Written alongside the feature rather than as an afterthought, for the same
reason the phase-5 tile test is written alongside the claim transaction --
these are the tests most likely to be quietly wrong, because a check-then-
insert in application code looks correct in every sequential test and only
fails under real contention.

The guarantee lives in the database (``uq_users_email_lower``), not in this
code. The service merely translates the resulting IntegrityError into a
domain error. These tests are what prove that translation happens and that
no second row is ever created.

Threads with a barrier are used rather than asyncio because the whole data
layer is synchronous psycopg; real contention means real OS threads holding
real row locks.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from app.db.session import get_session_factory
from app.models.user import User
from app.services.auth_service import (
    EmailAlreadyRegistered,
    authenticate,
    create_user,
)
from tests.conftest import VALID_EMAIL, VALID_PASSWORD

THREADS = 8


def _race(count: int, email: str) -> tuple[list[int], list[BaseException]]:
    """Run ``count`` simultaneous signups for one email.

    Returns ``(winning_user_ids, errors)``. A barrier ensures the threads all
    arrive before any of them inserts, which maximises the overlap -- without
    it the test can pass purely because the calls happened to serialise.
    """
    barrier = threading.Barrier(count)
    winners: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def attempt() -> None:
        # A fresh session per thread: sharing one would serialise on a single
        # connection and the test would prove nothing.
        with get_session_factory()() as session:
            try:
                barrier.wait(timeout=15)
                user = create_user(session, email=email, password=VALID_PASSWORD)
            except EmailAlreadyRegistered as exc:
                with lock:
                    errors.append(exc)
            except BaseException as exc:  # noqa: BLE001 - recorded for assertion
                with lock:
                    errors.append(exc)
            else:
                with lock:
                    winners.append(user.id)

    with ThreadPoolExecutor(max_workers=count) as pool:
        list(pool.map(lambda _: attempt(), range(count)))

    return winners, errors


def test_concurrent_signup_of_same_email_has_exactly_one_winner() -> None:
    winners, errors = _race(THREADS, VALID_EMAIL)

    assert len(winners) == 1, f"expected 1 winner, got {len(winners)}"
    assert len(errors) == THREADS - 1
    # Every loser must fail for the *expected* reason. A thread dying on a
    # deadlock or a connection error would still count as a loser here and
    # hide a real bug.
    assert all(isinstance(exc, EmailAlreadyRegistered) for exc in errors), errors


def test_concurrent_signup_creates_exactly_one_row() -> None:
    _race(THREADS, VALID_EMAIL)

    with get_session_factory()() as session:
        count = session.scalar(
            select(func.count())
            .select_from(User)
            .where(func.lower(User.email) == VALID_EMAIL)
        )
    assert count == 1


def test_concurrent_signup_of_distinct_emails_all_succeed() -> None:
    """The mirror image: the unique index must not serialise unrelated
    signups into failures."""
    emails = [f"racer{i}@example.com" for i in range(THREADS)]

    barrier = threading.Barrier(THREADS)
    results: list[int | BaseException] = []
    lock = threading.Lock()

    def attempt(email: str) -> None:
        with get_session_factory()() as session:
            try:
                barrier.wait(timeout=15)
                user = create_user(session, email=email, password=VALID_PASSWORD)
                outcome: int | BaseException = user.id
            except BaseException as exc:  # noqa: BLE001
                outcome = exc
            with lock:
                results.append(outcome)

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(attempt, emails))

    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, failures
    assert len({r for r in results if isinstance(r, int)}) == THREADS

    with get_session_factory()() as session:
        count = session.scalar(select(func.count()).select_from(User))
    assert count == THREADS


def test_concurrent_login_of_one_account_succeeds_for_every_caller() -> None:
    """Read-mostly path under contention.

    Login must not mutate the user row in a way that makes concurrent
    authentication fail or serialise behind a lock -- it is expected to run
    once per client session, so it is the hottest read in the system.
    """
    with get_session_factory()() as session:
        create_user(session, email=VALID_EMAIL, password=VALID_PASSWORD)

    barrier = threading.Barrier(THREADS)
    results: list[int | BaseException] = []
    lock = threading.Lock()

    def attempt() -> None:
        with get_session_factory()() as session:
            try:
                barrier.wait(timeout=15)
                user = authenticate(
                    session, email=VALID_EMAIL, password=VALID_PASSWORD
                )
                outcome: int | BaseException = user.id
            except BaseException as exc:  # noqa: BLE001
                outcome = exc
            with lock:
                results.append(outcome)

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(lambda _: attempt(), range(THREADS)))

    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, failures
    assert len(set(results)) == 1


def test_dummy_hash_path_is_used_for_unknown_email() -> None:
    """An unknown email must cost the same as a wrong password.

    Guards the enumeration defence: if this regresses, response latency
    discloses which addresses are registered.
    """
    import time

    with get_session_factory()() as session:
        create_user(session, email=VALID_EMAIL, password=VALID_PASSWORD)

        def timed(email: str, password: str) -> float:
            start = time.perf_counter()
            with pytest.raises(Exception):
                authenticate(session, email=email, password=password)
            return time.perf_counter() - start

        # Warm the module-level dummy hash so the first call is not the
        # outlier we are measuring.
        timed("warmup@example.com", VALID_PASSWORD)
        timed("warmup@example.com", VALID_PASSWORD)

        wrong_password = timed(VALID_EMAIL, "definitely-not-the-password")
        unknown_email = timed("nobody-here@example.com", "definitely-not-the-password")

    # bcrypt at rounds=4 has real jitter, so this is a loose bound. Its job
    # is to catch a regression to "unknown email returns immediately", which
    # would be orders of magnitude faster.
    assert unknown_email > wrong_password * 0.2, (
        f"unknown email ({unknown_email:.4f}s) returned far faster than a wrong "
        f"password ({wrong_password:.4f}s); enumeration defence may be broken"
    )
