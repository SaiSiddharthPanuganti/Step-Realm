"""Mana: passive regeneration, walking rewards, and the daily cap.

The regeneration and reward arithmetic is tested against the service with an
explicit ``now`` so every case is exact and instantaneous -- no sleeping, no
flakiness. The endpoint behaviour is then tested over HTTP against the real
clock.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.constants import (
    DAILY_WALKING_MANA_CAP,
    INITIAL_MANA,
    MANA_MAX,
    MANA_PER_STEP_BLOCK,
    MANA_REGEN_AMOUNT,
    MANA_REGEN_PERIOD_MINUTES,
    STEPS_PER_MANA_BLOCK,
)
from app.services import mana_service
from app.services.mana_service import (
    apply_passive_regen,
    apply_walking_steps,
    next_daily_reset,
    settle_mana,
)
from tests.conftest import VALID_EMAIL

T0 = mana_service.utcnow().replace(microsecond=0)
REGEN_PERIOD = timedelta(minutes=MANA_REGEN_PERIOD_MINUTES)


def a_user(**overrides):
    """A minimal stand-in with the mana columns the service touches.

    A tiny local object rather than an ORM User so the arithmetic tests do not
    need a database round trip.
    """

    class _User:
        def __init__(self) -> None:
            self.mana = 0
            self.last_mana_regen_at = T0
            self.daily_mana_earned = 0
            self.daily_mana_reset_at = T0 + timedelta(days=1)
            self.pending_step_remainder = 0

    user = _User()
    for key, value in overrides.items():
        setattr(user, key, value)
    return user


# ---------------------------------------------------------------------------
# passive regeneration
# ---------------------------------------------------------------------------


def test_no_elapsed_time_grants_nothing() -> None:
    user = a_user()
    assert apply_passive_regen(user, T0) == 0
    assert user.mana == 0


def test_one_second_short_of_a_period_grants_nothing() -> None:
    user = a_user()
    assert apply_passive_regen(user, T0 + REGEN_PERIOD - timedelta(seconds=1)) == 0
    assert user.mana == 0


def test_exactly_one_period_grants_five() -> None:
    user = a_user()
    assert apply_passive_regen(user, T0 + REGEN_PERIOD) == MANA_REGEN_AMOUNT
    assert user.mana == 5


def test_six_periods_grant_thirty() -> None:
    user = a_user()
    assert apply_passive_regen(user, T0 + REGEN_PERIOD * 6) == 30
    assert user.mana == 30


def test_watermark_advances_by_whole_periods_only() -> None:
    """Partial progress must survive.

    A player who checks in every 6 minutes is 4 minutes short of a period each
    time. If the watermark snapped to ``now`` they would never accumulate a
    single point of regen.
    """
    user = a_user()
    now = T0 + REGEN_PERIOD * 2 + timedelta(minutes=4)

    gained = apply_passive_regen(user, now)
    assert gained == 10
    assert user.last_mana_regen_at == T0 + REGEN_PERIOD * 2
    # 4 minutes of partial progress is still banked: 6 more minutes reaches
    # the third period.
    assert apply_passive_regen(user, now + timedelta(minutes=6)) == (
        MANA_REGEN_AMOUNT
    )
    assert user.mana == 15


def test_regen_is_capped_at_mana_max() -> None:
    user = a_user(mana=MANA_MAX)
    assert apply_passive_regen(user, T0 + REGEN_PERIOD * 100) == 0
    assert user.mana == MANA_MAX


def test_regen_partially_absorbed_by_cap() -> None:
    user = a_user(mana=MANA_MAX - 3)
    gained = apply_passive_regen(user, T0 + REGEN_PERIOD * 2)
    assert gained == 3
    assert user.mana == MANA_MAX


def test_regen_is_not_banked_while_over_cap() -> None:
    """The anti-hoarding rule.

    A player who stays at the cap while offline must accrue nothing, or
    "come back to a full bar" stops meaning "walked for it".
    """
    user = a_user(mana=MANA_MAX)
    apply_passive_regen(user, T0 + timedelta(days=7))
    assert user.mana == MANA_MAX

    # The watermark did advance, so spending now cannot retroactively grant
    # the week's regen.
    user.mana = 0
    assert apply_passive_regen(user, T0 + timedelta(days=7) + REGEN_PERIOD) == (
        MANA_REGEN_AMOUNT
    )
    assert user.mana == 5


def test_regen_never_advances_the_watermark_past_now() -> None:
    user = a_user()
    now = T0 + REGEN_PERIOD * 3 + timedelta(minutes=7)
    apply_passive_regen(user, now)
    assert user.last_mana_regen_at <= now


# ---------------------------------------------------------------------------
# daily reset
# ---------------------------------------------------------------------------


def test_daily_counter_is_zeroed_past_the_boundary() -> None:
    user = a_user(daily_mana_earned=180)
    outcome = settle_mana(user, now=T0 + timedelta(days=1, seconds=1))

    assert outcome.daily_reset_performed is True
    assert user.daily_mana_earned == 0
    assert user.daily_mana_reset_at > T0 + timedelta(days=1)


def test_daily_counter_survives_before_the_boundary() -> None:
    user = a_user(daily_mana_earned=180)
    outcome = settle_mana(user, now=T0 + timedelta(hours=5))

    assert outcome.daily_reset_performed is False
    assert user.daily_mana_earned == 180


def test_multi_day_gap_jumps_straight_to_the_next_boundary() -> None:
    user = a_user(daily_mana_earned=200)
    settle_mana(user, now=T0 + timedelta(days=9))

    assert user.daily_mana_earned == 0
    # One boundary after the return, not nine increments.
    assert next_daily_reset(T0 + timedelta(days=9)) == user.daily_mana_reset_at


def test_next_daily_reset_is_strictly_in_the_future() -> None:
    boundary = next_daily_reset(T0)
    assert boundary > T0
    # Calling it at the boundary itself must yield the following day.
    assert next_daily_reset(boundary) == boundary + timedelta(days=1)


# ---------------------------------------------------------------------------
# walking reward
# ---------------------------------------------------------------------------


def test_one_full_block_grants_twenty() -> None:
    user = a_user()
    gained, forfeited_cap, forfeited_max = apply_walking_steps(
        user, STEPS_PER_MANA_BLOCK, T0
    )
    assert gained == MANA_PER_STEP_BLOCK
    assert forfeited_cap == 0
    assert forfeited_max == 0
    assert user.mana == 20
    assert user.pending_step_remainder == 0
    assert user.daily_mana_earned == 20


def test_one_step_short_grants_nothing_but_keeps_the_steps() -> None:
    user = a_user()
    gained, _, _ = apply_walking_steps(user, STEPS_PER_MANA_BLOCK - 1, T0)
    assert gained == 0
    assert user.pending_step_remainder == STEPS_PER_MANA_BLOCK - 1


def test_fractional_blocks_carry_forward() -> None:
    """1,500 steps is 20 mana *and* 500 banked steps.

    Discarding the fraction would cost a real player up to 19 mana on every
    sync, which is why migration 0002 added the remainder column.
    """
    user = a_user()
    gained, _, _ = apply_walking_steps(user, 1_500, T0)
    assert gained == MANA_PER_STEP_BLOCK
    assert user.pending_step_remainder == 500

    gained, _, _ = apply_walking_steps(user, 500, T0)
    assert gained == MANA_PER_STEP_BLOCK
    assert user.pending_step_remainder == 0
    assert user.mana == 40


def test_accumulating_across_many_syncs_reaches_the_same_total() -> None:
    """Whether you sync once or a hundred times must not change the payout."""
    bulk = a_user()
    apply_walking_steps(bulk, 3_700, T0)

    drip = a_user()
    for _ in range(37):
        apply_walking_steps(drip, 100, T0)

    assert bulk.mana == drip.mana == 60
    assert bulk.pending_step_remainder == drip.pending_step_remainder == 700


def test_zero_steps_grants_nothing() -> None:
    user = a_user()
    gained, _, _ = apply_walking_steps(user, 0, T0)
    assert gained == 0
    assert user.mana == 0


# ---------------------------------------------------------------------------
# daily walking cap
# ---------------------------------------------------------------------------


def test_daily_cap_allows_exactly_ten_blocks() -> None:
    user = a_user()
    gained, forfeited, _ = apply_walking_steps(
        user, STEPS_PER_MANA_BLOCK * 10, T0
    )
    assert gained == DAILY_WALKING_MANA_CAP
    assert forfeited == 0
    assert user.daily_mana_earned == DAILY_WALKING_MANA_CAP


def test_eleventh_block_is_forfeited() -> None:
    user = a_user()
    apply_walking_steps(user, STEPS_PER_MANA_BLOCK * 10, T0)
    gained, forfeited, _ = apply_walking_steps(user, STEPS_PER_MANA_BLOCK, T0)

    assert gained == 0
    assert forfeited == MANA_PER_STEP_BLOCK
    assert user.mana == DAILY_WALKING_MANA_CAP


def test_partial_headroom_does_not_grant_a_partial_block() -> None:
    """Blocks are indivisible: 10 mana of headroom buys nothing."""
    user = a_user(daily_mana_earned=DAILY_WALKING_MANA_CAP - 10)
    gained, forfeited, _ = apply_walking_steps(user, STEPS_PER_MANA_BLOCK, T0)

    assert gained == 0
    assert forfeited == MANA_PER_STEP_BLOCK


def test_remainder_still_carries_while_the_cap_is_exhausted() -> None:
    user = a_user(daily_mana_earned=DAILY_WALKING_MANA_CAP)
    gained, forfeited, _ = apply_walking_steps(user, 600, T0)

    assert gained == 0
    assert forfeited == 0
    assert user.pending_step_remainder == 600


def test_cap_resets_and_earnings_resume() -> None:
    user = a_user(daily_mana_earned=DAILY_WALKING_MANA_CAP)
    settle_mana(user, now=T0 + timedelta(days=1, seconds=1))
    gained, _, _ = apply_walking_steps(user, STEPS_PER_MANA_BLOCK, T0)
    assert gained == MANA_PER_STEP_BLOCK


# ---------------------------------------------------------------------------
# interaction with MANA_MAX
# ---------------------------------------------------------------------------


def test_mana_earned_above_the_cap_is_still_counted_against_the_daily_cap() -> None:
    """The anti-bypass rule.

    Counting only mana that *landed* would let a player sitting at MANA_MAX
    bank a full extra daily allowance invisibly and then have a full bar the
    instant they spend 20 on a tile.
    """
    user = a_user(mana=MANA_MAX)
    gained, _, forfeited_max = apply_walking_steps(user, 1_000, T0)

    assert gained == MANA_PER_STEP_BLOCK
    assert user.mana == MANA_MAX
    assert forfeited_max == MANA_PER_STEP_BLOCK
    assert user.daily_mana_earned == MANA_PER_STEP_BLOCK


def test_capped_earnings_cannot_be_redeemed_after_spending() -> None:
    """End-to-end proof of the previous rule.

    Walk 10,000 steps against a full bar, then spend 20 on a tile. The player
    must be left near zero, not handed a fresh full bar.
    """
    user = a_user(mana=MANA_MAX)
    apply_walking_steps(user, 10_000, T0)
    assert user.mana == MANA_MAX
    assert user.daily_mana_earned == DAILY_WALKING_MANA_CAP

    user.mana -= 20  # as a tile claim would
    assert user.mana == 180

    # The day's allowance is spent, so no more walking mana today.
    gained, _, _ = apply_walking_steps(user, 1_000, T0)
    assert gained == 0
    assert user.mana == 180


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------


def test_regen_and_walking_combine_in_one_settlement() -> None:
    user = a_user(mana=0)
    outcome = settle_mana(
        user, steps=1_000, now=T0 + REGEN_PERIOD * 2
    )
    assert outcome.passive_gained == 10
    assert outcome.walking_gained == 20
    assert outcome.total_gained == 30
    assert user.mana == 30


def test_settlement_never_exceeds_mana_max() -> None:
    user = a_user(mana=MANA_MAX - 10)
    settle_mana(user, steps=5_000, now=T0 + REGEN_PERIOD * 4)
    assert user.mana == MANA_MAX


def test_outcome_reports_forfeited_mana_max() -> None:
    user = a_user(mana=MANA_MAX)
    outcome = settle_mana(user, steps=1_000, now=T0)
    assert outcome.mana_forfeited_to_mana_max == MANA_PER_STEP_BLOCK
    assert outcome.walking_gained == MANA_PER_STEP_BLOCK


def test_naive_datetime_is_rejected() -> None:
    from datetime import datetime

    user = a_user()
    with pytest.raises(ValueError, match="naive datetime"):
        settle_mana(user, now=datetime(2026, 1, 1, 12, 0, 0))


def test_negative_steps_are_rejected() -> None:
    user = a_user()
    with pytest.raises(ValueError, match="non-negative"):
        apply_walking_steps(user, -1, T0)


# ---------------------------------------------------------------------------
# HTTP: POST /mana/sync
# ---------------------------------------------------------------------------


def test_sync_requires_authentication(client) -> None:
    assert client.post("/mana/sync", json={"steps_delta": 1000}).status_code == 401


def test_sync_credits_a_full_block(client, authed) -> None:
    authed()
    response = client.post(
        "/mana/sync", json={"steps_delta": 1000}, headers=authed()
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["walking_mana_gained"] == 20
    assert body["total_mana_gained"] == 20
    assert body["mana"] == INITIAL_MANA + 20
    assert body["mana_max"] == MANA_MAX
    assert body["pending_step_remainder"] == 0
    assert body["daily_mana_earned"] == 20
    assert body["daily_mana_remaining"] == DAILY_WALKING_MANA_CAP - 20


def test_sync_returns_full_state_for_offline_reconciliation(client, authed) -> None:
    """The client must be able to replace its whole display from this one
    response, per the spec's reconciliation requirement."""
    authed()
    body = client.post(
        "/mana/sync", json={"steps_delta": 1_500}, headers=authed()
    ).json()

    for field in (
        "mana",
        "mana_max",
        "passive_mana_gained",
        "walking_mana_gained",
        "total_mana_gained",
        "steps_credited",
        "pending_step_remainder",
        "daily_mana_earned",
        "daily_mana_remaining",
        "daily_reset_at",
        "mana_forfeited_to_daily_cap",
        "mana_forfeited_to_mana_max",
    ):
        assert field in body, field


def test_sync_rejects_a_negative_delta(client, authed) -> None:
    authed()
    response = client.post(
        "/mana/sync", json={"steps_delta": -5}, headers=authed()
    )
    assert response.status_code == 422


def test_sync_rejects_an_absurd_delta(client, authed, no_step_speed_cap) -> None:
    from app.core.constants import MAX_STEPS_PER_SYNC

    authed()
    assert (
        client.post(
            "/mana/sync",
            json={"steps_delta": MAX_STEPS_PER_SYNC + 1},
            headers=authed(),
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/mana/sync",
            json={"steps_delta": MAX_STEPS_PER_SYNC},
            headers=authed(),
        ).status_code
        == 200
    )


def test_sync_persists_the_remainder_across_calls(
    client, authed, no_step_speed_cap
) -> None:
    from sqlalchemy import text as sql_text

    from app.db.session import get_session_factory
    from app.services.auth_service import normalise_email

    authed()
    client.post("/mana/sync", json={"steps_delta": 1_500}, headers=authed())
    client.post("/mana/sync", json={"steps_delta": 500}, headers=authed())

    with get_session_factory()() as read_session:
        row = read_session.execute(
            sql_text(
                "SELECT mana, pending_step_remainder FROM users "
                "WHERE email = :email"
            ),
            {"email": normalise_email(VALID_EMAIL)},
        ).mappings().one()

    assert row["pending_step_remainder"] == 0
    assert row["mana"] == INITIAL_MANA + 40


# ---------------------------------------------------------------------------
# HTTP: GET /me reflects regeneration
# ---------------------------------------------------------------------------


def test_me_shows_regen_accrued_while_the_app_was_closed(
    client, make_user, age_user
) -> None:
    """The reconciliation path the spec asks for.

    A client that was closed for an hour must be able to trust /me's number
    instead of replaying elapsed time locally.
    """
    from app.core.security import create_access_token

    user_id = make_user(mana=0)
    age_user(user_id, by=timedelta(hours=1))
    token, _ = create_access_token(user_id)

    body = client.get("/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert body["mana"] == 30
    assert body["mana_max"] == MANA_MAX


def test_me_settles_regen_persistently(
    client, make_user, age_user, user_state
) -> None:
    from app.core.security import create_access_token

    user_id = make_user(mana=0)
    age_user(user_id, by=timedelta(hours=1))
    token, _ = create_access_token(user_id)

    client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert user_state(user_id)["mana"] == 30

    # The watermark moved, so a second read grants nothing more.
    again = client.get("/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert again["mana"] == 30
