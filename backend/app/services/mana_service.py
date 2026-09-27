"""Mana: lazy passive regeneration, walking rewards, and the daily cap.

Design rules
------------
* **No scheduler.** Regeneration is a pure function of
  ``(mana, last_mana_regen_at, now)`` evaluated on read and on write. That is
  always correct, needs no cron, and cannot silently stop working when a
  scheduler dies.
* **Overflow is discarded, not banked.** A player at ``MANA_MAX`` who stays
  offline for a week accrues nothing. This is why the watermark advances even
  when the gain is entirely absorbed by the cap.
* **Postgres is the only authority.** Nothing here caches mana anywhere else.

Transaction ownership
---------------------
:func:`sync_mana` owns its transaction and takes a row lock, because two
concurrent syncs reading the same ``pending_step_remainder`` and
``daily_mana_earned`` would otherwise both grant mana for the same steps.
:func:`settle_mana` deliberately does *not* commit, so a caller that already
holds the lock (phase 5's claim transaction) can fold mana settlement into the
same transaction as the tile write.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import (
    DAILY_WALKING_MANA_CAP,
    MANA_MAX,
    MANA_PER_STEP_BLOCK,
    MANA_REGEN_AMOUNT,
    MANA_REGEN_PERIOD_MINUTES,
    STEPS_PER_MANA_BLOCK,
)
from app.models.user import User

logger = logging.getLogger(__name__)

REGEN_PERIOD = timedelta(minutes=MANA_REGEN_PERIOD_MINUTES)


class UserNotFound(Exception):
    """The user vanished between authentication and the locked read."""


class ImplausibleStepCount(Exception):
    """A step delta no real walking could have covered in the elapsed time.

    Carries the numbers so the API can tell the player what was plausible rather
    than refusing without explanation.
    """

    def __init__(self, steps: int, max_plausible: int) -> None:
        super().__init__(
            f"{steps} steps is not plausible; at most {max_plausible} could have "
            "been walked since your last sync"
        )
        self.steps = steps
        self.max_plausible = max_plausible


#: Floor on the elapsed time used for the speed check, in seconds.
#:
#: Without it, two syncs arriving microseconds apart measure against a
#: near-zero window and an allowance of essentially zero steps, so a player who
#: foregrounds the app and syncs immediately gets rejected for a walk that
#: really happened. One second is the smallest honest unit here: it admits a
#: handful of steps twice in a row and still refuses a tight loop submitting
#: thousands.
_MIN_ELAPSED_SECONDS = 1.0

#: How much walking a player who has never synced steps is assumed to have done.
#:
#: A deliberate hole, and the same trade as ``INITIAL_MANA``: a brand new
#: account is measured from ``created_at`` rather than treated as unlimited, and
#: without this the first sync of a real player's first session would be measured
#: against the seconds since they tapped "sign up" and rejected. Ten minutes buys
#: 3,000 steps at the cap -- a full mana block and some change -- so the
#: concession is small, and it is granted exactly once, at account creation,
#: rather than being a standing allowance.
_FIRST_SYNC_GRACE_SECONDS = 600.0


@dataclass(frozen=True)
class ManaOutcome:
    """What a settlement did, itemised.

    Returned to the client so it can reconcile after an offline gap instead
    of trusting local state -- and so a player can see *why* a walk earned
    less than expected.
    """

    passive_gained: int
    walking_gained: int
    steps_credited: int
    """Steps actually absorbed. Equal to the submitted delta, unless the
    daily cap left no room for the resulting blocks."""

    mana_forfeited_to_daily_cap: int
    """Mana lost because the player had already hit
    ``DAILY_WALKING_MANA_CAP`` today. Whole blocks only -- a partial block
    is always carried forward, never forfeited."""

    mana_forfeited_to_mana_max: int
    """Mana earned but absorbed by ``MANA_MAX`` because the bar was already
    full. Deliberately still counted against the daily cap; see
    :meth:`settle_mana`."""

    daily_reset_performed: bool

    @property
    def total_gained(self) -> int:
        return self.passive_gained + self.walking_gained


# ---------------------------------------------------------------------------
# Clock helpers
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    """Timezone-aware current time.

    Every instant in the mana system must be tz-aware. The engine forces the
    session to UTC on connect, so values read back from Postgres are aware;
    constructing aware values here keeps writes symmetric.
    """
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    """Reject naive datetimes rather than silently assuming a zone.

    A naive datetime here means some caller built it with ``datetime.now()``
    without tzinfo. Attaching UTC silently would hide a real bug; comparing
    naive to aware raises a confusing ``TypeError`` deep inside arithmetic
    instead. Fail while the stack is still useful.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            f"naive datetime rejected: {value!r}. Mana arithmetic requires a "
            "timezone-aware value; use app.services.mana_service.utcnow()."
        )
    return value.astimezone(timezone.utc)


def next_daily_reset(now: datetime | None = None) -> datetime:
    """The next instant at which ``daily_mana_earned`` returns to zero.

    Strictly greater than ``now``, so an account created exactly at the reset
    instant gets a full day rather than one that expires immediately.
    """
    from app.core.constants import DAILY_RESET_UTC_HOUR

    now = ensure_utc(now) if now is not None else utcnow()

    candidate = now.replace(
        hour=DAILY_RESET_UTC_HOUR, minute=0, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


# ---------------------------------------------------------------------------
# Individual rules
# ---------------------------------------------------------------------------


def roll_daily_reset(user: User, now: datetime) -> bool:
    """Zero the daily walking counter if the reset boundary has passed.

    Jumps straight to the next boundary after ``now`` rather than advancing a
    day at a time, so a user returning after a fortnight gets one clean
    window instead of a loop over missed days.
    """
    if now < user.daily_mana_reset_at:
        return False
    user.daily_mana_earned = 0
    user.daily_mana_reset_at = next_daily_reset(now)
    return True


def apply_passive_regen(user: User, now: datetime) -> int:
    """Grant mana for whole elapsed regen periods. Returns mana actually gained.

    The watermark advances by *whole periods only*, preserving partial
    progress, so a player syncing every 6 minutes is not permanently 4 minutes
    short of a period.
    """
    elapsed = now - user.last_mana_regen_at
    if elapsed < REGEN_PERIOD:
        return 0

    whole_periods = int(elapsed // REGEN_PERIOD)
    potential = whole_periods * MANA_REGEN_AMOUNT

    # Advance the watermark even when the cap will absorb the entire gain.
    # Not doing so would let a player bank unlimited regen while idle.
    user.last_mana_regen_at += whole_periods * REGEN_PERIOD

    before = user.mana
    user.mana = min(user.mana + potential, MANA_MAX)
    gained = user.mana - before

    if gained < potential:
        logger.debug(
            "passive regen partly absorbed by cap",
            extra={"potential": potential, "gained": gained},
        )
    return gained


def apply_walking_steps(
    user: User, steps: int, now: datetime
) -> tuple[int, int, int]:
    """Credit a step delta.

    Returns ``(mana_gained, forfeited_to_daily_cap, forfeited_to_mana_max)``.

    Fractional blocks carry forward via ``pending_step_remainder``; whole
    blocks that the daily cap cannot accommodate are dropped, because
    carrying them would let a player bank a buffer and spend it all at once on
    a later day.
    """
    if steps < 0:
        raise ValueError("step delta must be non-negative")

    total_steps = user.pending_step_remainder + steps
    whole_blocks = total_steps // STEPS_PER_MANA_BLOCK
    user.pending_step_remainder = total_steps % STEPS_PER_MANA_BLOCK

    headroom = DAILY_WALKING_MANA_CAP - user.daily_mana_earned
    granted_blocks = min(whole_blocks, max(headroom, 0) // MANA_PER_STEP_BLOCK)
    forfeited_to_cap = (whole_blocks - granted_blocks) * MANA_PER_STEP_BLOCK
    mana_gained = granted_blocks * MANA_PER_STEP_BLOCK

    # Count what was *earned* against the daily cap, not what fitted in the
    # bar. If the cap only counted mana that actually landed, a player sitting
    # at MANA_MAX could earn a further 200 mana into a full bar and then have
    # a full bar the instant they spent 20 on a tile -- a straight bypass of
    # the daily limit.
    user.daily_mana_earned += mana_gained

    before = user.mana
    user.mana = min(user.mana + mana_gained, MANA_MAX)
    forfeited_to_max = mana_gained - (user.mana - before)

    return mana_gained, forfeited_to_cap, forfeited_to_max


def step_allowance(user: User, now: datetime) -> int:
    """How many steps could plausibly have been walked since the last sync.

    ``MAX_STEPS_PER_SYNC`` bounds a single request but cannot tell a legitimate
    afternoon's walk from a scripted one, because the same 250,000 is fine once
    an hour and absurd ten times a minute. Only the ratio to elapsed time
    separates those, and the elapsed time is entirely server-side: the baseline
    is a column this service writes, never a timestamp from the request.

    ``created_at`` is NOT NULL and ``last_step_sync_at`` is nullable, so
    ``last_step_sync_at or created_at`` is always a datetime. There is no
    "no baseline" case to handle, and inventing one would only add a path that
    can never be exercised.
    """
    settings = get_settings()
    baseline = user.last_step_sync_at or user.created_at

    elapsed = (now - baseline).total_seconds()
    if user.last_step_sync_at is None:
        # First sync. The account's own age is the honest baseline, but a player
        # who signs up and immediately syncs a walk has seconds of elapsed time
        # and a real delta, so the floor grants a grace period rather than
        # refusing them.
        elapsed = max(elapsed, _FIRST_SYNC_GRACE_SECONDS)
    elapsed = max(elapsed, _MIN_ELAPSED_SECONDS)

    return int(elapsed * settings.max_steps_per_second)


def check_step_plausibility(
    user: User, steps: int, *, now: datetime | None = None
) -> None:
    """Raise :class:`ImplausibleStepCount` if ``steps`` could not have been walked.

    Not a security control -- a client that walks honestly could still lie about
    how far. It makes the blatant version fail loudly instead of paying out, and
    it is the first rule in the codebase that reasons about a step count being
    *impossible* rather than merely *large*.
    """
    if steps <= 0:
        return
    now = ensure_utc(now) if now is not None else utcnow()
    max_plausible = step_allowance(user, now)
    if steps > max_plausible:
        raise ImplausibleStepCount(steps, max_plausible)


def advance_step_baseline(user: User, now: datetime) -> None:
    """Record that steps were just credited, starting the next allowance window.

    Called only on the success path, and only when ``steps`` was non-zero -- see
    the note on ``User.last_step_sync_at`` for why a rejected or zero-step sync
    must leave the baseline alone.
    """
    user.last_step_sync_at = now


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------


def settle_mana(
    user: User, *, steps: int = 0, now: datetime | None = None
) -> ManaOutcome:
    """Bring one user up to date. Mutates ``user``; does **not** commit.

    Ordering: daily reset, then passive regen, then walking. Passive regen is
    applied first so the walking reward is what fills whatever headroom is
    left, which matches the intuition that regen accrued over time and the
    walk is the thing happening now. The two are otherwise independent -- the
    daily cap governs walking mana only.
    """
    now = ensure_utc(now) if now is not None else utcnow()

    reset_performed = roll_daily_reset(user, now)
    passive_gained = apply_passive_regen(user, now)
    walking_gained, forfeited_cap, forfeited_max = apply_walking_steps(
        user, steps, now
    )

    return ManaOutcome(
        passive_gained=passive_gained,
        walking_gained=walking_gained,
        steps_credited=steps,
        mana_forfeited_to_daily_cap=forfeited_cap,
        mana_forfeited_to_mana_max=forfeited_max,
        daily_reset_performed=reset_performed,
    )


def load_user_for_update(session: Session, user_id: int) -> User:
    """Re-read a user under a row lock.

    Two details here are load-bearing.

    ``of=User`` -- ``User.college`` is ``lazy="joined"``, so the statement
    carries a ``LEFT OUTER JOIN colleges``. PostgreSQL rejects a bare
    ``FOR UPDATE`` on the nullable side of an outer join, so the unqualified
    form raises NotSupportedError and the endpoint 500s. Scoping the lock with
    ``of=User`` emits ``FOR UPDATE OF users``, which locks the row we care
    about and leaves the join alone. It is also the honest expression of
    intent: we want the user row, not the college row.

    ``populate_existing`` -- the object may already sit in the session's
    identity map from ``get_current_user``, loaded *without* a lock. Without
    this option SQLAlchemy would hand back that stale instance and the lock
    would guard a row whose values we never actually read.
    """
    statement = (
        select(User)
        .where(User.id == user_id)
        .with_for_update(of=User)
        .execution_options(populate_existing=True)
    )
    try:
        return session.execute(statement).scalar_one()
    except NoResultFound as exc:
        raise UserNotFound(user_id) from exc


def sync_mana(
    session: Session, user_id: int, steps: int, *, now: datetime | None = None
) -> tuple[User, ManaOutcome]:
    """Settle mana and credit steps in one locked transaction. Commits.

    This is the only correct way to apply a step delta. Two concurrent syncs
    without the lock would read the same remainder and daily counters and both
    grant mana for the same steps.

    The plausibility check runs under the same lock and before anything is
    mutated, so a rejected sync leaves no trace: not the step remainder, not the
    mana, and not the speed baseline. The last of those matters most -- a
    rejection that advanced the baseline would shrink the player's allowance
    every time they tripped it.
    """
    now = ensure_utc(now) if now is not None else utcnow()
    locked = load_user_for_update(session, user_id)
    check_step_plausibility(locked, steps, now=now)
    outcome = settle_mana(locked, steps=steps, now=now)
    if steps > 0:
        advance_step_baseline(locked, now)
    session.commit()
    return locked, outcome


def refresh_mana(
    session: Session, user: User, *, now: datetime | None = None
) -> tuple[User, ManaOutcome]:
    """Bring a user up to date for a *read* path, and persist it.

    This makes GET /me a writer, which deserves justification: a read that
    does not settle mana would show a stale balance, and worse, an unlocked
    read racing a /mana/sync could overwrite the sync's result with a stale
    value -- a genuine lost update. Taking the same row lock as the sync
    serialises them.

    The cost is that every profile view serialises per user, which is
    negligible at one request per app foreground.
    """
    locked = load_user_for_update(session, user.id)
    outcome = settle_mana(locked, steps=0, now=now)
    session.commit()
    return locked, outcome
