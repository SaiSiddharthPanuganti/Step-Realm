"""Display names: the derivation rules, and the uniqueness guarantee.

Two halves, tested separately on purpose:

* the pure functions in :mod:`app.services.display_name`, which are a function
  of their input and need no database, and
* the retry loop in :func:`app.services.auth_service.create_user`, whose whole
  job is to turn "that name is taken" into "here is a different one" without
  ever losing the signup.

The second half is the one that can actually break in production, and it breaks
under concurrency, so it gets a barrier-based race rather than a sequential
simulation. A sequential test of a uniqueness retry proves very little: it
cannot tell "reacts correctly to a conflict" from "gets lucky".
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.constants import (
    DISPLAY_NAME_MAX_ATTEMPTS,
    DISPLAY_NAME_MAX_LENGTH,
)
from app.db.session import get_session_factory
from app.models.user import User
from app.services import display_name as dn
from app.services.auth_service import (
    DisplayNameUnavailable,
    EmailAlreadyRegistered,
    create_user,
)
from tests.conftest import VALID_PASSWORD


# ---------------------------------------------------------------------------
# Sanitisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("alice", "alice"),
        ("Alice.Smith", "Alice_Smith"),
        # The plus-tag is provider bookkeeping and says nothing about the
        # player, so it is dropped rather than shown on their territory.
        ("alice+walk@x.com".split("@")[0], "alice_walk"),
        ("a-b-c", "a_b_c"),
        ("__lead_and_trail__", "lead_and_trail"),
        ("a...b", "a_b"),
        ("alice!!smith", "alice_smith"),
        # Non-ASCII is replaced, not transliterated: "_n_cod_", and the
        # surrounding underscores are then trimmed to "n_cod".
        ("ünïcodé", "n_cod"),
        ("1234", "1234"),
    ],
)
def test_sanitise_produces_a_legal_stem(raw: str, expected: str) -> None:
    assert dn.sanitise(raw) == expected


def test_sanitised_names_only_contain_allowed_characters() -> None:
    """The column is a plain VARCHAR, so the *rules* are the only enforcement.

    Anything outside the allowed set would have to survive a round trip through
    a map label, a URL and a terminal, and the migration's backfill assumes the
    same character class as the application.
    """
    nasty = "a b\tc\nd\"e'f/g\\h|i;j:k,l.m<n>o?p@q#r$s%t^u&v*w(x)y[z]{|}"
    result = dn.sanitise(nasty)
    assert set(result) <= dn.ALLOWED_CHARACTERS


def test_sanitise_never_exceeds_the_column_length() -> None:
    long_local_part = "x" * (DISPLAY_NAME_MAX_LENGTH * 3)
    assert len(dn.sanitise(long_local_part)) == DISPLAY_NAME_MAX_LENGTH


def test_sanitise_is_idempotent() -> None:
    """Re-sanitising an already-legal name must not change it.

    This matters because the migration backfills with its own SQL version of
    these rules. If the two implementations disagree, a migrated name could
    come out differently from the one the app would have generated.
    """
    for raw in ("alice", "Alice.Smith", "a...b", "  spaced  out  ", "a-b-c"):
        once = dn.sanitise(raw)
        assert dn.sanitise(once) == once


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("alice@example.com", "alice"),
        ("Alice.Smith@Example.com", "Alice_Smith"),
        ("alice+walk@example.com", "alice_walk"),
        # A local part that sanitises to nothing usable falls back rather than
        # producing a one- or two-character name.
        ("a@example.com", "walker"),
        ("__@example.com", "walker"),
        ("1@example.com", "walker"),
    ],
)
def test_stem_from_email(email: str, expected: str) -> None:
    assert dn.stem_from_email(email) == expected


def test_stem_never_leaks_the_domain() -> None:
    """The domain is not part of the player's identity, and for some providers
    it is the name of their school or employer."""
    assert "gmail" not in dn.stem_from_email("alice.walker@gmail.com")


def test_stem_preserves_casing_but_signup_normalises_first() -> None:
    """Names derived over HTTP are always lowercase, and that is deliberate.

    ``SignupRequest`` lowercases the email before the service sees it, so by
    the time a name is derived the original casing is already gone. Rather than
    preserve it -- which would mean keeping the raw address around and holding
    two sources of truth for one field -- the name inherits the normalised form.

    It also removes a whole class of problem: two players whose addresses
    differ only in case get visibly different labels, and no label can be
    capitalised to impersonate a differently-cased one.
    """
    # The pure function is case-preserving.
    assert dn.stem_from_email("Alice.Smith@Example.com") == "Alice_Smith"

    # Signup sees only the lowercased address, so the wire result is lowercase.
    with get_session_factory()() as session:
        user = create_user(
            session, email="Alice.Smith@Example.com", password=VALID_PASSWORD
        )
    assert user.display_name == "alice_smith"


def test_fallback_is_used_only_when_too_short() -> None:
    assert dn.stem_from_email("al@example.com") == "walker"  # 2 chars -> fallback
    assert dn.stem_from_email("alp@example.com") == "alp"  # 3 chars -> kept


# ---------------------------------------------------------------------------
# Suffixes
# ---------------------------------------------------------------------------


def test_suffix_keeps_the_result_within_the_length_limit() -> None:
    stem = "x" * DISPLAY_NAME_MAX_LENGTH
    for _ in range(200):
        candidate = dn.with_suffix(stem)
        assert len(candidate) <= DISPLAY_NAME_MAX_LENGTH
        assert candidate.startswith(stem[:10])


def test_suffix_is_random_not_sequential() -> None:
    """A sequential suffix would be trivially guessable, and -- worse -- the
    same on every reinstall. This is not asserting secrecy, only that the
    function is actually drawing from a pool rather than counting."""
    assert len({dn.random_suffix() for _ in range(200)}) > 190


def test_first_attempt_has_no_suffix_and_later_ones_do() -> None:
    """A distinctive email should get a clean name, not "alice_a1b2"."""
    stem = dn.stem_from_email("alice@example.com")
    assert dn.candidate_for_attempt(stem, 0) == stem
    assert dn.candidate_for_attempt(stem, 1) != stem
    assert dn.candidate_for_attempt(stem, 1).startswith("alice_")


def test_candidate_refuses_to_exceed_the_retry_budget() -> None:
    """The loop is bounded; this is the belt to that braces.

    An unbounded retry would hang a request thread forever if the uniqueness
    check were ever broken, which is a far worse failure than an error.
    """
    with pytest.raises(ValueError, match="no display name available"):
        dn.candidate_for_attempt("alice", DISPLAY_NAME_MAX_ATTEMPTS)


# ---------------------------------------------------------------------------
# Signup integration
# ---------------------------------------------------------------------------


def test_signup_assigns_a_derived_display_name(client: TestClient) -> None:
    response = client.post(
        "/auth/signup",
        json={"email": "Alice.Smith@example.com", "password": VALID_PASSWORD},
    )
    assert response.status_code == 201
    assert response.json()["user"]["display_name"] == "alice_smith"


def test_display_name_is_visible_to_the_player_on_me(client: TestClient) -> None:
    """The owner needs to see what their label is, or they cannot recognise
    their own territory and cannot report that someone else took it."""
    signup = client.post(
        "/auth/signup",
        json={"email": "viewer@example.com", "password": VALID_PASSWORD},
    ).json()

    me = client.get(
        "/me", headers={"Authorization": f"Bearer {signup['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["display_name"] == "viewer"


def test_a_taken_display_name_gets_a_suffixed_one() -> None:
    """Two different addresses, same local part. The second must not fail."""
    with get_session_factory()() as session:
        first = create_user(session, email="alice@x.com", password=VALID_PASSWORD)
        first_name = first.display_name

    with get_session_factory()() as session:
        second = create_user(session, email="alice@y.com", password=VALID_PASSWORD)
        second_name = second.display_name

    assert first_name == "alice"
    assert second_name != first_name
    assert second_name.startswith("alice_")


def test_a_taken_name_is_retried_even_when_the_casing_differs() -> None:
    """The unique index is on ``lower(display_name)``.

    A pre-check written with ``==`` would let "Alice" through as available
    because "alice" is not byte-identical to it, and the insert would then be
    rejected by the index with no retry. Going through the index means the
    exact comparison used here is the one that decides.
    """
    with get_session_factory()() as session:
        create_user(session, email="Alice@x.com", password=VALID_PASSWORD)

    with get_session_factory()() as session:
        other = create_user(session, email="alice@y.com", password=VALID_PASSWORD)

    assert other.display_name != "Alice"


def test_exhausting_the_candidates_raises_a_distinct_error(make_user) -> None:
    """Distinguishing this from "email taken" is a privacy property.

    If both mapped to the same error, a caller could learn from a 409 that
    *some other player* already holds the name their address would have
    produced -- turning signup into an oracle for other players' names.
    """
    from app.services import auth_service

    make_user(email="alice@x.com")  # takes the bare stem
    make_user(email="holder@x.com", display_name="alice_fixd")  # takes a suffix

    original = dn.random_suffix
    dn.random_suffix = lambda: "fixd"  # type: ignore[assignment]
    try:
        with get_session_factory()() as session:
            with pytest.raises(DisplayNameUnavailable):
                create_user(session, email="alice@z.com", password=VALID_PASSWORD)
    finally:
        dn.random_suffix = original  # type: ignore[assignment]

    assert DisplayNameUnavailable is not EmailAlreadyRegistered
    assert auth_service.DISPLAY_NAME_CONSTRAINT == "uq_users_display_name_lower"


def test_exhausted_candidates_surface_as_503_not_409(
    client: TestClient, make_user
) -> None:
    """503 says "retry", and leaks nothing about anyone else's name.

    The second signup shares its *local part* with the first, so it collides on
    the bare stem; with the suffix pinned, every retry collides too.
    """
    original = dn.random_suffix
    dn.random_suffix = lambda: "0000"  # type: ignore[assignment]
    try:
        assert (
            client.post(
                "/auth/signup",
                json={"email": "taken@x.com", "password": VALID_PASSWORD},
            ).status_code
            == 201
        )
        make_user(email="holder@x.com", display_name="taken_0000")

        response = client.post(
            "/auth/signup",
            json={"email": "taken@y.com", "password": VALID_PASSWORD},
        )
    finally:
        dn.random_suffix = original  # type: ignore[assignment]

    assert response.status_code == 503
    # The detail must not confirm that the contested name exists.
    assert "taken" not in response.json()["detail"]


def test_the_password_is_hashed_once_across_retries(monkeypatch) -> None:
    """bcrypt is deliberately expensive.

    Hashing inside the retry loop would make a name collision cost 250ms per
    attempt, so a contended signup could take seconds. Counting the calls
    pins the optimisation: the digest is computed before the loop and reused.
    """
    from app.services import auth_service

    calls = 0
    real_hash = auth_service.hash_password

    def counting_hash(password: str) -> str:
        nonlocal calls
        calls += 1
        return real_hash(password)

    monkeypatch.setattr(auth_service, "hash_password", counting_hash)

    with get_session_factory()() as session:
        create_user(session, email="once@x.com", password=VALID_PASSWORD)
    with get_session_factory()() as session:
        create_user(session, email="alice@x.com", password=VALID_PASSWORD)
    with get_session_factory()() as session:
        create_user(session, email="alice@y.com", password=VALID_PASSWORD)

    # Three signups, one of which had to retry a name collision.
    assert calls == 3


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

THREADS = 8


def _race_distinct_emails_same_stem(count: int) -> tuple[list[str], list[BaseException]]:
    """Simultaneous signups whose emails all reduce to the same display name.

    This is the case the retry loop exists for. The local part is identical
    across the threads and only the *domain* varies, so all of them derive the
    same candidate and every thread's first attempt collides with the others.
    Correctness then depends entirely on the index rejecting the losers and the
    loop moving on to a suffixed candidate.

    Varying the local part instead (``racer0@``, ``racer1@``, ...) would look
    equivalent and prove nothing here, because distinct local parts derive
    distinct names and there is no collision to resolve.
    """
    barrier = threading.Barrier(count)
    names: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        with get_session_factory()() as session:
            try:
                barrier.wait(timeout=15)
                user = create_user(
                    session,
                    email=f"racer@host{index}.example.com",
                    password=VALID_PASSWORD,
                )
            except BaseException as exc:  # noqa: BLE001 - recorded for assertion
                with lock:
                    errors.append(exc)
            else:
                with lock:
                    names.append(user.display_name)

    with ThreadPoolExecutor(max_workers=count) as pool:
        list(pool.map(attempt, range(count)))

    return names, errors


def test_concurrent_same_stem_signups_all_get_distinct_names() -> None:
    names, errors = _race_distinct_emails_same_stem(THREADS)

    assert not errors, f"unexpected failures: {errors}"
    assert len(names) == THREADS
    assert len(set(names)) == THREADS, f"duplicate display names assigned: {names}"


def test_concurrent_same_stem_signups_all_succeed() -> None:
    """Distinct names are not enough -- every signup must have gone through.

    A retry loop that resolved the collision by *failing* the loser would also
    produce distinct names, just with errors attached.
    """
    names, errors = _race_distinct_emails_same_stem(THREADS)
    assert errors == []
    assert len(names) == THREADS


def test_concurrent_same_stem_signups_leave_no_duplicate_rows() -> None:
    """Belt and braces: the names are distinct *and* the table agrees.

    Distinct returned names could still hide a duplicate if the loser reused a
    stale object after rollback, so this checks the authoritative table.
    """
    _race_distinct_emails_same_stem(THREADS)

    with get_session_factory()() as session:
        rows = session.execute(
            select(User.email, func.lower(User.display_name))
            .where(User.email.like("racer@host%.example.com"))
        ).all()

    assert len(rows) == THREADS
    assert len({lowered for _, lowered in rows}) == THREADS


def test_concurrent_signup_of_one_email_still_has_one_winner() -> None:
    """Adding the display-name retry must not weaken the email guarantee.

    A loser can now fail twice over -- once on the name, once on the email --
    so this pins that the retry does not swallow the email conflict and let a
    second account through.
    """
    barrier = threading.Barrier(THREADS)
    winners: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def attempt() -> None:
        with get_session_factory()() as session:
            try:
                barrier.wait(timeout=15)
                user = create_user(
                    session, email="same@example.com", password=VALID_PASSWORD
                )
            except BaseException as exc:  # noqa: BLE001 - recorded for assertion
                with lock:
                    errors.append(exc)
            else:
                with lock:
                    winners.append(user.id)

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(lambda _: attempt(), range(THREADS)))

    assert len(winners) == 1
    # Losers may legitimately fail on either constraint -- whichever the index
    # happens to check first -- but every one must still end up as
    # EmailAlreadyRegistered, never as a DisplayNameUnavailable. A name-budget
    # exhaustion here would mean the retry loop is spinning on a conflict it
    # should have reported immediately.
    assert all(isinstance(exc, EmailAlreadyRegistered) for exc in errors), errors


# ---------------------------------------------------------------------------
# Database-level guarantees
# ---------------------------------------------------------------------------


def test_display_name_uniqueness_is_case_insensitive_in_the_database() -> None:
    """The guarantee lives in the index, not in application code.

    Inserted with raw SQL precisely to bypass every layer that could be
    masking a missing constraint: no Pydantic validation, no service
    pre-check, no schema default.
    """
    from sqlalchemy import text

    with get_session_factory()() as session:
        session.execute(
            text(
                "INSERT INTO users (email, password_hash, display_name, mana, "
                "last_mana_regen_at, daily_mana_earned, daily_mana_reset_at) "
                "VALUES ('a@x.com', 'x', 'Impostor', 0, now(), 0, now())"
            )
        )
        session.commit()

        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO users (email, password_hash, display_name, mana, "
                    "last_mana_regen_at, daily_mana_earned, daily_mana_reset_at) "
                    "VALUES ('b@x.com', 'x', 'impostor', 0, now(), 0, now())"
                )
            )
        session.rollback()


def test_display_name_is_not_nullable() -> None:
    from sqlalchemy import text

    with get_session_factory()() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO users (email, password_hash, mana, "
                    "last_mana_regen_at, daily_mana_earned, daily_mana_reset_at) "
                    "VALUES ('noname@x.com', 'x', 0, now(), 0, now())"
                )
            )
        session.rollback()
