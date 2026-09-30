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

The faucet
----------
The only active mana faucet is walking distance. :func:`record_walk
<app.services.walk_service.record_walk>` owns its transaction and takes a row
lock, and folds mana settlement into the same transaction that validates and
ledgers the walk, so the metres that buy a claim are the metres the server
itself measured -- the client's only input is a raw GPS distance.

Transaction ownership
---------------------
:func:`settle_mana` deliberately does *not* commit, so a caller that already
holds the lock (a walk report, or phase 5's claim transaction) can fold mana
settlement into the same transaction as the write it is already doing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from app.core.constants import (
    DAILY_WALKING_MANA_CAP,
    MANA_MAX,
    MANA_PER_METER,
    MANA_REGEN_AMOUNT,
    MANA_REGEN_PERIOD_MINUTES,
)
from app.models.user import User

logger = logging.getLogger(__name__)

REGEN_PERIOD = timedelta(minutes=MANA_REGEN_PERIOD_MINUTES)


class UserNotFound(Exception):
    """The user vanished between authentication and the locked read."""


@dataclass(frozen=True)
class ManaOutcome:
    """What a settlement did, itemised.

    Returned to the client so it can reconcile after an offline gap instead
    of trusting local state -- and so a player can see *why* a walk earned
    less than expected.
    """

    passive_gained: int
    walking_gained: int
    walk_meters_credited: float
    """The distance this settlement priced. Equal to the metres the caller
    validated, whatever the cap did to the resulting mana."""

    mana_forfeited_to_daily_cap: int
    """Mana lost because the player had already hit
    ``DAILY_WALKING_MANA_CAP`` today. Whole mana only -- any sub-mana metre
    fraction vanishes on the floor, which at 1 mana per metre is at most half
    a mana per report."""

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


def apply_walking_metres(
    user: User, metres: float, now: datetime
) -> tuple[int, int, int]:
    """Credit validated walking distance.

    Returns ``(mana_gained, forfeited_to_daily_cap, forfeited_to_mana_max)``.

    Distance earns ``MANA_PER_METER`` mana per metre, truncated to whole mana
    per report (at 1:1 the fractional loss is at most one mana). Over a day the
    ``MANA_MAX`` bar is filled before the daily cap, and the order they bite in
    matters:

    * Distance that the daily cap cannot accommodate is dropped, because
      carrying it would let a player bank a buffer and spend it all at once on
      a later day.
    * A ``walk_meters`` value is never refundable: the ledger records what was
      walked, the client reports a distance, and there is no step count for a
      malicious client to inflate.
    """
    if metres < 0:
        raise ValueError("walk distance must be non-negative")

    earned = int(metres * MANA_PER_METER)

    headroom = DAILY_WALKING_MANA_CAP - user.daily_mana_earned
    granted = min(earned, max(headroom, 0))
    forfeited_to_cap = earned - granted

    # Count what was *earned* toward the daily cap, not what fitted in the bar.
    # If the cap only counted mana that actually landed, a player sitting at
    # MANA_MAX could earn a further 2000 mana into a full bar and then have a
    # full bar the instant they spent 20 on a tile -- a straight bypass of the
    # daily limit.
    user.daily_mana_earned += granted

    before = user.mana
    user.mana = min(user.mana + granted, MANA_MAX)
    forfeited_to_max = granted - (user.mana - before)

    return granted, forfeited_to_cap, forfeited_to_max


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------


def settle_mana(
    user: User, *, walk_meters: float = 0.0, now: datetime | None = None
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
    walking_gained, forfeited_cap, forfeited_max = apply_walking_metres(
        user, walk_meters, now
    )

    return ManaOutcome(
        passive_gained=passive_gained,
        walking_gained=walking_gained,
        walk_meters_credited=walk_meters,
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


def refresh_mana(
    session: Session, user: User, *, now: datetime | None = None
) -> tuple[User, ManaOutcome]:
    """Bring a user up to date for a *read* path, and persist it.

    This makes GET /me a writer, which deserves justification: a read that
    does not settle mana would show a stale balance, and worse, an unlocked
    read racing a walk report could overwrite the report's result with a stale
    value -- a genuine lost update. Taking the same row lock as the walk
    serialises them.

    The cost is that every profile view serialises per user, which is
    negligible at one request per app foreground.
    """
    locked = load_user_for_update(session, user.id)
    outcome = settle_mana(locked, walk_meters=0.0, now=now)
    session.commit()
    return locked, outcome
