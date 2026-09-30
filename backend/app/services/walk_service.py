"""Walk distance: the metres a player covers, credited server-side.

The rule the whole module exists to enforce is the product decision: a tile is
only claimable once the player has covered ``min_claim_distance_meters`` (20 by
default) *inside* it, and the number that decides that must be one the server
wrote itself. The same metres are the game's one active mana faucet: every
metre the server validates pays ``MANA_PER_METER`` mana, so walking the 20 m
inside a hex earns exactly the mana that claiming it costs.

Transaction ownership
---------------------
:func:`record_walk` owns its transaction and takes the same ``users`` row lock
that mana settlement takes, for the same two reasons: two concurrent reports
would otherwise both read the same baseline timestamp and each could claim the
same elapsed window, and the plausibility check and the baseline advance must
happen against a consistent snapshot. The mana for the walk is settled under
this same lock and committed with it, so the distance that funds a claim is
the distance the server itself measured -- the client's only input is a raw
GPS distance.

``load_user_for_update`` and ``settle_mana`` are imported from
``mana_service`` rather than duplicated: every write path that can change a
balance is serialised against this one lock, and reusing it is what keeps that
true.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import (
    DAILY_WALKING_MANA_CAP,
    MANA_MAX,
    MAX_WALK_PROGRESS_METERS,
    MAX_WALK_REPORT_METERS,
    MAX_WALK_SPEED_METERS_PER_SECOND,
)
from app.models.walk_progress import WalkProgress
from app.services import mana_service
from app.services.geo import Axial, lonlat_to_axial

logger = logging.getLogger(__name__)

#: Floor on the elapsed time used for the speed check, in seconds. Without it,
#: two reports microseconds apart measure against a near-zero window and an
#: allowance of zero metres, so a client that reports while standing still gets
#: rejected for a walk that really happened. Same rationale as the mana
#: service's settlement windows.
_MIN_ELAPSED_SECONDS = 1.0

#: How much walking a player who has never reported is assumed to have done.
#: The first report is measured from ``created_at`` until the baseline exists,
#: and an account that signs up and walks straight into a hex has only its own
#: age to be measured against, so the floor grants a short grace period. It is
#: granted exactly once, not as a standing allowance.
_FIRST_REPORT_GRACE_SECONDS = 30.0


class ImplausibleWalkReport(Exception):
    """A reported distance no real walking could have covered in the elapsed time.

    Carries the numbers so the API can tell the player what was plausible rather
    than refusing without explanation.
    """

    def __init__(self, meters: float, max_plausible: float) -> None:
        super().__init__(
            f"{meters:.1f} m walked is not plausible; at most {max_plausible:.1f} "
            "m could have been covered since your last report"
        )
        self.meters = meters
        self.max_plausible = max_plausible


@dataclass(frozen=True)
class WalkReportResult:
    """What a walk report did to the ledger and the balance.

    Returned to the client so the walking player sees the mana they earned,
    plus the state it left the balance in, without a second round trip.
    """

    q: int
    r: int
    total: float
    """Accumulated metres in the walked hex toward ``min_claim_distance_meters``."""

    mana: int
    mana_max: int
    walking_gained: int
    passive_gained: int
    daily_mana_earned: int
    daily_mana_remaining: int


# ---------------------------------------------------------------------------
# Clock helpers (re-exported so the walk service speaks the same calendar as
# the mana system rather than building its own).
# ---------------------------------------------------------------------------

utcnow = mana_service.utcnow
ensure_utc = mana_service.ensure_utc


def _report_allowance(user, now: datetime) -> float:
    """How many metres could plausibly have been walked since the last report.

    Mirrors :func:`mana_service.step_allowance` exactly: the baseline is a
    column the service writes (``last_walk_report_at``), never a timestamp from
    the request, and the first report falls back to ``created_at`` with a grace
    floor so a brand-new session is not refused for an honest first walk.
    """
    baseline = user.last_walk_report_at or user.created_at
    elapsed = (now - baseline).total_seconds()
    if user.last_walk_report_at is None:
        elapsed = max(elapsed, _FIRST_REPORT_GRACE_SECONDS)
    elapsed = max(elapsed, _MIN_ELAPSED_SECONDS)
    return elapsed * MAX_WALK_SPEED_METERS_PER_SECOND


def check_report_plausibility(
    user, meters: float, *, now: datetime | None = None
) -> None:
    """Raise :class:`ImplausibleWalkReport` if ``meters`` could not have been walked."""
    if meters <= 0:
        raise ImplausibleWalkReport(meters, 0.0)
    now = ensure_utc(now) if now is not None else utcnow()
    max_plausible = _report_allowance(user, now)
    if meters > max_plausible:
        raise ImplausibleWalkReport(meters, max_plausible)


def record_walk(
    session: Session,
    *,
    user_id: int,
    lat: float,
    lon: float,
    meters: float,
    now: datetime | None = None,
) -> WalkReportResult:
    """Credit ``meters`` to the hex containing ``(lat, lon)`` and to the balance.

    Returns the ledger total plus the mana state after settlement. Commits, and
    owns its transaction: the plausibility check, the ledger write, the mana
    settlement and the baseline advance are one locked unit.

    The attribution is deliberately server-side: the client sends the
    coordinate its distance window started at, the server projects it to a hex
    with the same ``lonlat_to_axial`` every other endpoint uses, and the two
    cannot disagree about which tile a metre landed in.

    Mana is earned on every metre the server *validates* -- ``meters`` after
    the plausibility checks, not the hex ledger total. A walk that crosses a
    hex border still yields its distance, while the per-hex progress ledger
    stays the claim-gate question it has always been.
    """
    now = ensure_utc(now) if now is not None else utcnow()
    locked = mana_service.load_user_for_update(session, user_id)

    # Reject before mutating anything, so a bad report leaves no trace -- not
    # in the ledger, not in the balance, not in the baseline.
    if meters > MAX_WALK_REPORT_METERS:
        raise ImplausibleWalkReport(meters, MAX_WALK_REPORT_METERS)
    check_report_plausibility(locked, meters, now=now)

    # The metres were plausible and are therefore genuinely walked: convert
    # them to mana under the same lock and commit. Settlement also runs the
    # passive-regen watermark and the daily reset, so a walk report is a full
    # balance refresh, exactly like a claim is.
    outcome = mana_service.settle_mana(locked, walk_meters=meters, now=now)

    q, r = lonlat_to_axial(lat, lon)

    # Upsert: create the row on first report, add to it on later ones. The
    # unique index makes two concurrent first reports serialise rather than
    # producing two rows.
    current = session.scalar(
        select(WalkProgress.meters).where(
            WalkProgress.user_id == user_id,
            WalkProgress.q == q,
            WalkProgress.r == r,
        )
    )
    current = 0.0 if current is None else float(current)
    total = min(current + meters, MAX_WALK_PROGRESS_METERS)

    session.execute(
        pg_insert(WalkProgress)
        .values(user_id=user_id, q=q, r=r, meters=total, updated_at=now)
        .on_conflict_do_update(
            index_elements=["user_id", "q", "r"],
            set_={"meters": total, "updated_at": now},
        )
    )

    # Advance the baseline only now, on the success path, and to the report's
    # own time: the next report's allowance is measured from here.
    locked.last_walk_report_at = now
    session.commit()

    return WalkReportResult(
        q=q,
        r=r,
        total=total,
        mana=locked.mana,
        mana_max=MANA_MAX,
        walking_gained=outcome.walking_gained,
        passive_gained=outcome.passive_gained,
        daily_mana_earned=locked.daily_mana_earned,
        daily_mana_remaining=max(DAILY_WALKING_MANA_CAP - locked.daily_mana_earned, 0),
    )


def progress_meters(session: Session, user_id: int, q: int, r: int) -> float:
    """How far this player has walked inside ``(q, r)``. Zero if never reported."""
    meters = session.scalar(
        select(WalkProgress.meters).where(
            WalkProgress.user_id == user_id,
            WalkProgress.q == q,
            WalkProgress.r == r,
        )
    )
    return 0.0 if meters is None else float(meters)


def progress_map(
    session: Session, user_id: int, tiles: list[Axial]
) -> dict[Axial, float]:
    """Per-tile metres for the *viewer*, restricted to ``tiles``.

    The same range-scan shape as ``world_service.load_owners`` runs against
    ``hex_tiles``: a bounding range in q/r that the unique index serves, and an
    exact ``(q, r)`` key. Only the viewer's own ledger is ever returned.
    """
    if not tiles or not user_id:
        return {}

    q_values = [q for q, _ in tiles]
    r_values = [r for _, r in tiles]

    statement = (
        select(WalkProgress.q, WalkProgress.r, WalkProgress.meters)
        .where(
            WalkProgress.user_id == user_id,
            WalkProgress.q.between(min(q_values), max(q_values)),
            WalkProgress.r.between(min(r_values), max(r_values)),
        )
    )
    return {
        (q, r): float(meters)
        for q, r, meters in session.execute(statement)
    }


def clear_progress(session: Session, user_id: int, q: int, r: int) -> None:
    """Delete a player's progress in a hex. Does **not** commit.

    Called from inside the claim transaction: the moment a tile becomes yours
    the metres spent on it are gone, so re-walking (or re-walking-the-same-20m)
    is a fresh effort and the /world progress probe answers zero from now on.
    """
    session.execute(
        delete(WalkProgress).where(
            WalkProgress.user_id == user_id,
            WalkProgress.q == q,
            WalkProgress.r == r,
        )
    )