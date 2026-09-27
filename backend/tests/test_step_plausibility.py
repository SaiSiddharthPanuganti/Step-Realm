"""Walking-speed plausibility: refusing step counts that could not have happened.

``MAX_STEPS_PER_SYNC`` already bounds one request, but it cannot tell an
afternoon's walk from a scripted one -- the same 250,000 steps is ordinary once
an hour and absurd ten times a minute. Only the ratio to elapsed server-side time
separates those, and this file pins that down.

The property that is easy to get wrong, and so gets the most attention here, is
what a *rejected* sync must not do. If a refusal advanced the speed baseline, the
player would lose allowance every time they tripped the check, and a client with
a clock skew would find their allowance permanently shrunk.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: A fixed instant so every expectation is exact rather than approximate.
NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


def _user(make_user, **overrides):
    """Create a user with a known step baseline, as a live ORM object.

    ``created_at`` and ``last_step_sync_at`` are both set explicitly. A first
    sync gets a grace allowance keyed on ``created_at``; every later one is
    measured from ``last_step_sync_at``, and leaving either to the column default
    would make the test depend on how fast the test suite runs.
    """
    from app.db.session import get_session_factory
    from app.models.user import User

    data = {"created_at": NOW - timedelta(days=1)}
    data.update(overrides)
    user_id = make_user(**data)

    fresh = get_session_factory()()
    try:
        user = fresh.get(User, user_id)
        for key, value in data.items():
            setattr(user, key, value)
        fresh.commit()
        return user
    finally:
        fresh.close()


def _sync(session, user_id: int, steps: int, *, now=NOW):
    from app.services import mana_service

    return mana_service.sync_mana(session, user_id, steps, now=now)


# ---------------------------------------------------------------------------
# The allowance
# ---------------------------------------------------------------------------


def test_first_sync_gets_a_grace_allowance(make_user) -> None:
    """A brand new player has no baseline to measure against.

    Without a floor on the first sync, a player who signs up and syncs a second
    later would be judged against two seconds of elapsed time and refused --
    onboarding would be impossible, and a client that batched its first walk into
    one request would look like a cheater.
    """
    from app.services import mana_service

    rate = mana_service.get_settings().max_steps_per_second
    user = _user(make_user, last_step_sync_at=None, created_at=NOW)

    # Two seconds of real elapsed time, but the grace floor lifts it to 600.
    assert mana_service.step_allowance(user, NOW) == (
        mana_service._FIRST_SYNC_GRACE_SECONDS * rate
    )


def test_a_first_sync_is_measured_from_account_creation(make_user) -> None:
    """The baseline for a first sync is ``created_at``, not the sync time.

    An account that has existed for a day gets a day of allowance on its first
    sync. Keying the grace off the sync moment instead would make a player who
    reinstalled the app and re-synced unable to claim a walk they genuinely took.
    """
    from app.services import mana_service

    rate = mana_service.get_settings().max_steps_per_second
    elapsed = 3_600
    user = _user(
        make_user, last_step_sync_at=None, created_at=NOW - timedelta(seconds=elapsed)
    )

    assert mana_service.step_allowance(user, NOW) == elapsed * rate


def test_a_later_sync_is_measured_from_the_last_credited_sync(make_user) -> None:
    user = _user(
        make_user, last_step_sync_at=NOW - timedelta(seconds=100)
    )

    from app.services import mana_service

    allowed = mana_service.step_allowance(user, NOW)

    assert allowed == int(100 * mana_service.get_settings().max_steps_per_second)


def test_the_allowance_never_drops_below_one_second(make_user) -> None:
    """Two syncs in the same instant still get a token allowance.

    Without the floor the second would be refused with an allowance of zero and
    no client could ever sync twice quickly, which is a real pattern: a sync on
    app resume and one a few seconds later.
    """
    from app.services import mana_service

    user = _user(make_user, last_step_sync_at=NOW)

    allowed = mana_service.step_allowance(user, NOW)

    assert allowed == mana_service._MIN_ELAPSED_SECONDS * mana_service.get_settings().max_steps_per_second


def test_the_allowance_scales_with_the_configured_speed(
    make_user, settings_override
) -> None:
    """The ceiling is configuration, so the ceiling has to be honoured.

    Also the test that proves the limit is read per call rather than cached at
    import -- a module-level read would ignore the override and the anti-cheat
    would be unconfigurable after deployment.
    """
    from app.services import mana_service

    user = _user(make_user, last_step_sync_at=NOW - timedelta(seconds=10))

    settings_override(MAX_STEPS_PER_SECOND=2)
    slow = mana_service.step_allowance(user, NOW)
    settings_override(MAX_STEPS_PER_SECOND=20)
    fast = mana_service.step_allowance(user, NOW)

    assert slow == 20
    assert fast == 200


def test_a_baseline_always_exists() -> None:
    """``created_at`` NOT NULL is what makes the first-sync grace well-defined.

    Worth stating because the alternative is a nullable creation time, and then
    a player with no baseline would need a policy for an unmeasurable request --
    which is the dead ``baseline is None`` branch this replaced.
    """
    from sqlalchemy import text as sql_text

    from app.db.session import get_session_factory

    with get_session_factory()() as session:
        nullable = session.execute(
            sql_text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = 'created_at'"
            )
        ).scalar()
        baseline_nullable = session.execute(
            sql_text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = 'last_step_sync_at'"
            )
        ).scalar()

    assert nullable == "NO"
    # Nullable on purpose: NULL is what marks a player as never-synced.
    assert baseline_nullable == "YES"


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------


def test_an_implausible_delta_is_refused(make_user, session) -> None:
    from app.services import mana_service

    user = _user(make_user, last_step_sync_at=NOW - timedelta(seconds=1))

    with pytest.raises(mana_service.ImplausibleStepCount) as caught:
        _sync(session, user.id, 100_000)

    assert caught.value.steps == 100_000


def test_a_refused_sync_changes_nothing(make_user, session, user_state) -> None:
    """Mana, remainder and baseline all survive a refusal untouched.

    The baseline is the one that is easy to get wrong: advancing it on a
    rejection would shrink the player's allowance every time they tripped the
    check, so a client with a wrong clock would end up permanently unable to
    sync.
    """
    from app.services import mana_service

    user = _user(make_user, mana=100, last_step_sync_at=NOW - timedelta(seconds=1))
    before = user_state(user.id)

    with pytest.raises(mana_service.ImplausibleStepCount):
        _sync(session, user.id, 100_000)

    after = user_state(user.id)
    assert after["mana"] == before["mana"] == 100
    assert after["pending_step_remainder"] == before["pending_step_remainder"]
    assert after["last_step_sync_at"] == before["last_step_sync_at"] == (
        NOW - timedelta(seconds=1)
    )


def test_a_refusal_leaves_the_next_legitimate_sync_working(make_user, session) -> None:
    """A player is not punished for tripping the check.

    Because the baseline is untouched, the allowance after a refusal is the same
    as it was before, so a correct client recovers on its very next request.
    """
    from app.services import mana_service

    user = _user(make_user, mana=0, last_step_sync_at=NOW - timedelta(seconds=600))

    with pytest.raises(mana_service.ImplausibleStepCount):
        _sync(session, user.id, 10_000_000)

    recovered, outcome = _sync(session, user.id, 3_000, now=NOW + timedelta(seconds=1))

    assert outcome.steps_credited == 3_000
    assert recovered.mana > 0


def test_a_sync_at_exactly_the_allowance_is_accepted(make_user, session) -> None:
    """The boundary is inclusive, and it is pinned from both sides.

    One step over the allowance must be refused while exactly the allowance is
    accepted, otherwise a legitimate player is refused for a rounding accident
    and no amount of waiting helps them.
    """
    from app.services import mana_service

    cap = mana_service.get_settings().max_steps_per_second
    user = _user(make_user, mana=0, last_step_sync_at=NOW - timedelta(seconds=100))
    allowance = 100 * cap

    accepted, outcome = _sync(session, user.id, allowance)
    assert outcome.steps_credited == allowance

    session.expire_all()
    rejected_user = _user(
        make_user, email="second@example.com", mana=0,
        last_step_sync_at=NOW - timedelta(seconds=100),
    )
    with pytest.raises(mana_service.ImplausibleStepCount):
        _sync(session, rejected_user.id, allowance + 1)


def test_the_response_to_a_refused_sync_is_422(client, authed) -> None:
    """A 422 is the documented contract for an implausible count.

    400 would say the request was malformed, which it is not -- the client sent
    a well-formed number that simply could not be true.
    """
    authed()
    headers = authed()

    response = client.post("/mana/sync", json={"steps_delta": 250_000}, headers=headers)

    assert response.status_code == 422
    assert "steps" in response.text.lower() or "implausib" in response.text.lower()


def test_a_refused_sync_leaves_the_players_balance_intact(
    client, authed, user_state
) -> None:
    authed()
    headers = authed()
    client.post("/mana/sync", json={"steps_delta": 0}, headers=headers)

    before = user_state(1)
    refused = client.post(
        "/mana/sync", json={"steps_delta": 250_000}, headers=headers
    )
    after = user_state(1)

    assert refused.status_code == 422
    assert after["mana"] == before["mana"]
    assert after["pending_step_remainder"] == before["pending_step_remainder"]


# ---------------------------------------------------------------------------
# Baseline advancement
# ---------------------------------------------------------------------------


def test_a_credited_sync_advances_the_baseline(make_user, session) -> None:
    from app.db.session import get_session_factory
    from app.models.user import User

    user = _user(make_user, mana=0, last_step_sync_at=NOW - timedelta(seconds=100))
    _sync(session, user.id, 100)

    with get_session_factory()() as read_session:
        assert read_session.get(User, user.id).last_step_sync_at == NOW


def test_a_zero_step_sync_leaves_the_baseline_alone(make_user, session) -> None:
    """Otherwise polling would silently destroy a player's allowance.

    An app that syncs on every foreground, and a client that syncs twice for
    other reasons, would each be handing back walking budget by advancing a
    baseline that no steps were earned against.
    """
    from app.db.session import get_session_factory
    from app.models.user import User

    earlier = NOW - timedelta(seconds=100)
    user = _user(make_user, mana=0, last_step_sync_at=earlier)
    _sync(session, user.id, 0)

    with get_session_factory()() as read_session:
        assert read_session.get(User, user.id).last_step_sync_at == earlier


def test_repeated_zero_syncs_do_not_erode_the_allowance(
    make_user, session, settings_override
) -> None:
    """The end-to-end version: poll fifty times, then still walk normally.

    This is the test that would fail if the baseline advanced unconditionally.
    """
    settings_override(MAX_STEPS_PER_SECOND=5)
    user = _user(make_user, mana=0, last_step_sync_at=NOW - timedelta(seconds=600))

    for offset in range(50):
        _sync(session, user.id, 0, now=NOW + timedelta(seconds=offset * 0.01))

    # 600s of allowance at 5 steps/s, less the 0.49s the polling consumed.
    _sync(session, user.id, 2_500, now=NOW + timedelta(seconds=1))


def test_steps_forfeited_to_the_daily_cap_still_advance_the_baseline(
    make_user, session
) -> None:
    """Steps consumed by the cap were still walked.

    The daily cap is a mana limit, not a step limit: the player did the walking
    and the steps cannot be re-submitted. Leaving the baseline alone would let
    the same steps be presented again, which converts a daily mana cap into no
    cap at all.
    """
    from app.core.constants import DAILY_WALKING_MANA_CAP, MANA_PER_STEP_BLOCK

    user = _user(
        make_user,
        mana=0,
        last_step_sync_at=NOW - timedelta(seconds=100_000),
        daily_mana_earned=DAILY_WALKING_MANA_CAP,
    )
    allowance = 100_000 * 5

    _, outcome = _sync(session, user.id, allowance)

    assert outcome.walking_gained == 0
    assert outcome.mana_forfeited_to_daily_cap > 0
    assert outcome.mana_forfeited_to_daily_cap % MANA_PER_STEP_BLOCK == 0

    from app.db.session import get_session_factory
    from app.models.user import User

    with get_session_factory()() as read_session:
        assert read_session.get(User, user.id).last_step_sync_at == NOW


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_syncs_cannot_bypass_the_allowance(make_user) -> None:
    """The lock that makes the check meaningful.

    The plausibility test reads the baseline, and the baseline write happens
    later in the same transaction. Without ``FOR UPDATE`` on the user row, two
    concurrent syncs would both read the same old baseline and both be admitted
    -- the check would pass while being worth nothing, and the only sign would be
    mana appearing faster than a player can earn it.
    """
    from app.db.session import get_session_factory
    from app.services import mana_service

    user_id = make_user(
        mana=0, created_at=NOW - timedelta(days=1), last_step_sync_at=NOW
    )
    barrier = __import__("threading").Barrier(8)
    admitted = []

    def _attempt(_: int):
        with get_session_factory()() as session:
            barrier.wait(timeout=30)
            try:
                _sync(session, user_id, 1_000, now=NOW + timedelta(seconds=1))
                return True
            except mana_service.ImplausibleStepCount:
                return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = list(pool.map(_attempt, range(8)))

    # One second of elapsed time buys five steps; the eight threads ask for
    # 1,000 each. Serialising them can admit almost nothing.
    assert admitted.count(True) <= 1
    assert admitted.count(False) >= 7


def test_concurrent_legitimate_syncs_all_succeed(make_user) -> None:
    """The check must not become a throughput problem for honest clients.

    Eight syncs of a few steps each, spread over enough elapsed time to be
    plausible, all admitted. A limiter that refused these would be
    indistinguishable from a bug to the player, and a developer chasing it would
    be tempted to relax the very rule that protects the economy.
    """
    from app.db.session import get_session_factory
    from app.models.user import User

    user_id = make_user(
        mana=0, created_at=NOW - timedelta(days=1), last_step_sync_at=NOW
    )

    def _attempt(offset: int):
        with get_session_factory()() as session:
            _sync(session, user_id, 5, now=NOW + timedelta(seconds=60 * offset + 1))
            return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_attempt, range(8)))

    assert all(results)
    with get_session_factory()() as read_session:
        # 8 x 5 steps, all banked: none lost to a read-modify-write on the
        # remainder, which is what overlapping syncs would cause.
        assert read_session.get(User, user_id).pending_step_remainder == 40
