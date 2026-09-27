"""Fixed-window request throttling, backed by Postgres.

Why this module exists and what it is allowed to do
---------------------------------------------------
A rate limiter is a guard against abuse, not a correctness mechanism, and the
distinction drives every decision here: **when the limiter cannot make a
decision, it allows the request.** See :func:`enforce` for how that is enforced.

Why Postgres rather than Redis
-----------------------------
``app/db/redis.py`` establishes that Redis is "derived, best-effort" and that
nothing in it may "ever raise into a request path". That is the correct rule for
a leaderboard, which is display state Postgres can recompute. A limiter is not
display state: it *rejects* requests. Hosting it on the one dependency that is
contractually allowed to be absent would mean a Redis outage either silently
removes the limit or 429s every player in the game. Postgres has neither problem
and the atomic-upsert primitive below is the same one territory claims already
rely on, so this is one concurrency idiom in the codebase rather than two.

The algorithm
-------------
One atomic statement per request:

.. code-block:: sql

    INSERT INTO rate_limit_counters (subject, scope, window_started_at, count)
    VALUES (:subject, :scope, :now, 1)
    ON CONFLICT (subject, scope) DO UPDATE SET
        count = CASE WHEN window_started_at <= :cutoff
                     THEN 1 ELSE count + 1 END,
        window_started_at = CASE WHEN window_started_at <= :cutoff
                                 THEN :now ELSE window_started_at END
    RETURNING count, window_started_at

The ``ON CONFLICT DO UPDATE`` takes the row lock and releases it in the same
statement, so the increment is serialised by Postgres with no read-then-write
window for two requests to interleave through. That is the whole reason for
choosing an upsert over the ``SELECT ... FOR UPDATE`` pattern used in
``territory_service``: a limiter runs on *every* request including ones that will
be rejected, and a single round trip beats two.

Deliberately a fixed window, not a token bucket or a sliding log. A token bucket
is the better algorithm and would be the right choice for a service under real
load; it needs a periodic write to refill, and a rolling window needs either a
per-request row or a sorted-set of timestamps. The one-row-per-subject shape in
``rate_limit_counters`` is what keeps this table proportional to the number of
players. The accepted cost is boundary burst: a subject that spends its whole
allowance at the end of one window can spend it again at the start of the next,
so the worst case is ``2 * limit`` in a very short span. For an abuse guard
against a walking game that is a fine trade; it is not a fine trade for
something billing money, which is the distinction worth stating.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import case
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.rate_limit_counter import RateLimitCounter

logger = logging.getLogger(__name__)

#: Names for the limits that exist. A typo in a scope string would silently
#: create a brand new, empty bucket and disable that limit, so they are constants
#: rather than literals at the call sites.
SCOPE_CLAIM_BY_LOCATION = "claim_by_location"
SCOPE_AUTH_LOGIN = "auth_login"
SCOPE_AUTH_SIGNUP = "auth_signup"

#: Rows whose window closed more than this long ago are safe to drop: no
#: request can still be throttled by them, because the window they represent has
#: expired many times over. See :func:`purge_expired`.
RATE_LIMIT_RETENTION = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """The outcome of counting one request against one window."""

    allowed: bool
    limit: int
    remaining: int
    """Requests still available in this window. Never negative: a caller that
    formats this into a header has to be able to trust it."""
    retry_after_seconds: int
    """Seconds until the current window ends. Zero when allowed. Computed where
    the request's own ``now`` is still in scope, because the window is anchored
    on the first request rather than on a wall-clock boundary, so the reset time
    is not otherwise knowable."""
    window_started_at: datetime
    """Start of the window that was counted against."""


def enforce(
    session: Session,
    *,
    subject: str,
    scope: str,
    limit: int,
    window_seconds: int,
    now: datetime | None = None,
) -> RateLimitDecision:
    """Count one request against ``subject``'s window for ``scope``.

    Fails **open**. If the counter table is missing, the database is in a state
    this code does not understand, or the upsert raises for any reason the caller
    did not cause, the request is allowed and the failure is logged. A limiter
    that can take the game down has a far worse failure mode than a limiter that
    can be bypassed for a few minutes during an incident, and the protection it
    buys -- against credential stuffing and world-probing -- is not worth turning
    a database hiccup into an outage. The counter is observability-adjacent
    state, so nothing game-visible is lost by skipping it.

    The rollback in the failure path is not optional: after a failed statement
    Postgres leaves the transaction aborted, and every subsequent query on that
    session fails until it is rolled back. It is safe because callers pass a
    session dedicated to the limiter (see ``app.api.deps.rate_limit_session``)
    rather than the request handler's, so there is nothing else in flight to
    discard. Calling this with a session that has other pending work would throw
    that work away.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=window_seconds)

    # A window is stale once its start is at or before the cutoff. Written as a
    # SQL CASE rather than as raw text() with hand-bound parameters: a
    # ``text()`` fragment inside ``set_`` does not get its bind parameters
    # compiled by SQLAlchemy, and the resulting "a value is required for bind
    # parameter 'cutoff'" surfaces as a runtime error *inside* the fail-open
    # handler below -- so the limiter quietly allowed every request and looked
    # like it worked. Compiled expressions cannot fail that way.
    stale = RateLimitCounter.window_started_at <= cutoff

    try:
        row = session.execute(
            pg_insert(RateLimitCounter)
            .values(
                subject=subject,
                scope=scope,
                window_started_at=now,
                count=1,
            )
            .on_conflict_do_update(
                index_elements=[RateLimitCounter.subject, RateLimitCounter.scope],
                set_={
                    # Expired window restarts at 1; a live one keeps counting.
                    "count": case((stale, 1), else_=RateLimitCounter.count + 1),
                    "window_started_at": case(
                        (stale, now), else_=RateLimitCounter.window_started_at
                    ),
                },
            )
            .returning(RateLimitCounter.count, RateLimitCounter.window_started_at)
        ).one()
    except Exception:  # noqa: BLE001 - the whole point is to never propagate
        # ERROR, not WARNING: reaching here means a guard is not working, which
        # is an operational event. Logged loudly because the request succeeding
        # tells nobody -- the symptom is an absence.
        logger.error(
            "rate limiter unavailable, allowing request (fail-open)",
            exc_info=True,
            extra={"scope": scope, "subject": subject},
        )
        session.rollback()
        return RateLimitDecision(
            allowed=True,
            limit=limit,
            remaining=limit,
            retry_after_seconds=0,
            window_started_at=now,
        )

    count, window_started_at = row.count, row.window_started_at
    session.commit()

    allowed = count <= limit
    seconds_left = window_seconds - (now - window_started_at).total_seconds()

    return RateLimitDecision(
        allowed=allowed,
        limit=limit,
        remaining=max(0, limit - count),
        retry_after_seconds=0 if allowed else max(1, math.ceil(seconds_left)),
        window_started_at=window_started_at,
    )


def purge_expired(session: Session, *, now: datetime | None = None) -> int:
    """Drop rows whose window closed long enough ago to be irrelevant.

    Not called from the request path. The table is proportional to the number of
    distinct subjects rather than to traffic, so it does not need reaping to stay
    healthy -- but subjects who stop playing do accumulate, and this is the query
    to schedule (``DELETE FROM rate_limit_counters WHERE window_started_at < ?``)
    from cron or a periodic job. Returned count is for logging and tests.
    """
    now = now or datetime.now(timezone.utc)
    result = session.execute(
        RateLimitCounter.__table__.delete().where(
            RateLimitCounter.window_started_at
            < now - RATE_LIMIT_RETENTION
        )
    )
    session.commit()
    return result.rowcount or 0
