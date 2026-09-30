"""POST /territory/claim and POST /territory/claim-by-location.

Both require a bearer token: claiming spends mana and creates a permanent row
in the world, so it is never reachable anonymously.

The three rejection statuses are deliberately distinct. A client has to be able
to tell "somebody already owns this hex" (stop, pick another) from "walk a bit
more" (come back) from "claim next to your own land" (pick a different hex),
and a client branching on the status code alone gets that for free.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, limit_per_user
from app.core.config import get_settings
from app.core.constants import MANA_MAX, RATE_LIMIT_WINDOW_SECONDS
from app.db.session import get_db
from app.models.user import User
from app.schemas.territory import (
    ClaimByLocationRequest,
    ClaimRequest,
    ClaimResponse,
    ClaimedTile,
    WalkReportRequest,
    WalkReportResponse,
)
from app.services import (
    leaderboard_service,
    rate_limit_service,
    territory_service,
    walk_service,
    walkability_service,
)
from app.services.geo import lonlat_to_axial
from app.services.mana_service import UserNotFound

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/territory", tags=["territory"])


def _claim_by_location_limit():
    """Throttle for claim-by-location, the cheapest way to probe the world.

    Built by a factory because a throttling dependency has to be declared on the
    route, and the ceiling is configuration read per request (see
    ``deps.LimitSource``).
    """
    return limit_per_user(
        rate_limit_service.SCOPE_CLAIM_BY_LOCATION,
        lambda: get_settings().claim_location_rate_limit_per_minute,
        RATE_LIMIT_WINDOW_SECONDS,
    )


def _build_response(outcome: territory_service.ClaimOutcome) -> ClaimResponse:
    return ClaimResponse(
        tile=ClaimedTile(
            q=outcome.q,
            r=outcome.r,
            world_x=outcome.world_x,
            world_y=outcome.world_y,
            lat=outcome.lat,
            lon=outcome.lon,
            is_new=outcome.is_new,
        ),
        mana_spent=outcome.mana_spent,
        mana=outcome.mana,
        mana_max=MANA_MAX,
        owned_tile_count=outcome.owned_tile_count,
    )


def _ensure_walkable(q: int, r: int) -> None:
    """Refuse to materialise a hex that is not on walkable ground.

    Runs *before* ``claim_tile`` so the Overpass fetch never happens inside the
    claim's locked transaction (a network call holding a user row lock would
    serialise every other claim behind it) and so a refusal costs nothing.

    Degrades to allowing the claim when the data source is down, matching the
    map view's fail-open: during an outage the grid shows everywhere and
    anywhere is claimable, which is at least a coherent world.
    """
    if not get_settings().walkability_enabled:
        return
    try:
        walkable = walkability_service.walkability_for_claim(q, r)
    except walkability_service.WalkabilityUnavailable as exc:
        logger.warning("walkability unavailable; claim not restricted: %s", exc)
        return
    if not walkable:
        raise territory_service.NotWalkable


def _translate(exc: Exception) -> HTTPException:
    """Map a domain error to the status a client can act on."""
    if isinstance(exc, territory_service.TileAlreadyClaimed):
        # 409: the request was well formed but conflicts with existing state.
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That tile is already claimed by another player",
        )
    if isinstance(exc, territory_service.InsufficientMana):
        # 402 rather than 409 or 400. The request was valid and the player is
        # allowed to make it; they just cannot afford it right now, which is
        # exactly what Payment Required means. Folding it in with "tile taken"
        # would leave the client unable to tell the player to go walking.
        return HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=str(exc),
        )
    if isinstance(exc, territory_service.NotAdjacentToTerritory):
        # 422: structurally valid, but violates the adjacency rule.
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Claim a tile adjacent to territory you already own",
        )
    if isinstance(exc, territory_service.NotWalkable):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Hexes only appear where ground is walkable -- move to a road or path",
        )
    if isinstance(exc, territory_service.OutsideWorldLeash):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="That tile is outside the playable world",
        )
    if isinstance(exc, territory_service.NotEnoughWalkProgress):
        # 422 like its siblings: structurally valid, but the player has not yet
        # earned the right. The message carries how far off they are, and the
        # status lets a client branch on "keep walking" rather than treating it
        # as a failure.
        delta = exc.required - exc.walked
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Walk {exc.required:.0f} m inside this hex to claim it"
                f" -- {max(0, delta):.0f} m to go"
            ),
        )
    if isinstance(exc, UserNotFound):
        # Authenticated a moment ago, deleted since. Same generic 401 as any
        # other bad credential, so this cannot be used to confirm an id exists.
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    raise exc


@router.post(
    "/claim",
    response_model=ClaimResponse,
    summary="Claim a hex by axial coordinates",
)
def claim(
    payload: ClaimRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> ClaimResponse:
    """Claim one hex for the authenticated player.

    Idempotent: claiming a hex you already own reports success and spends
    nothing, so a client that retries after a dropped response is not charged
    twice and does not have to distinguish "already mine" from "lost to a
    rival".
    """
    try:
        _ensure_walkable(payload.q, payload.r)
        outcome = territory_service.claim_tile(
            session, user_id=user.id, q=payload.q, r=payload.r
        )
    except (  # noqa: B902 - _translate is total over this set
        territory_service.TileAlreadyClaimed,
        territory_service.InsufficientMana,
        territory_service.NotAdjacentToTerritory,
        territory_service.NotWalkable,
        territory_service.NotEnoughWalkProgress,
        territory_service.OutsideWorldLeash,
        UserNotFound,
    ) as exc:
        raise _translate(exc) from None

    if outcome.mana_gained_since_last_read:
        logger.info(
            "claim funded partly by regeneration",
            extra={
                "user_id": user.id,
                "regenerated": outcome.mana_gained_since_last_read,
            },
        )

    response = _build_response(outcome)
    # Best-effort: the leaderboard's sorted set is a cache, so a dropped write
    # only makes this entry stale until the next read repairs it.
    leaderboard_service.sync_tile_count(user.id, outcome.owned_tile_count)
    return response


@router.post(
    "/claim-by-location",
    response_model=ClaimResponse,
    summary="Claim the hex containing a GPS coordinate",
)
def claim_by_location(
    payload: ClaimByLocationRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
    _limit: None = Depends(_claim_by_location_limit()),
) -> ClaimResponse:
    """Claim whichever hex contains this point.

    The server resolves the coordinate to a hex, so the client never has to
    reimplement the projection and cannot disagree with the backend about
    which cell a position falls in.

    This is the throttled variant. ``POST /territory/claim`` is not, because it
    is already bounded by mana: a caller can only spend a claim they can afford,
    and the world it can reach is the world it already owns. This endpoint takes
    an arbitrary coordinate, which makes it the cheap way to enumerate the map.
    """
    q, r = lonlat_to_axial(payload.lat, payload.lon)
    try:
        _ensure_walkable(q, r)
        outcome = territory_service.claim_tile_at_location(
            session, user_id=user.id, lat=payload.lat, lon=payload.lon
        )
    except (
        territory_service.TileAlreadyClaimed,
        territory_service.InsufficientMana,
        territory_service.NotAdjacentToTerritory,
        territory_service.NotWalkable,
        territory_service.NotEnoughWalkProgress,
        territory_service.OutsideWorldLeash,
        UserNotFound,
    ) as exc:
        raise _translate(exc) from None

    response = _build_response(outcome)
    # Same best-effort cache write as the plain claim endpoint.
    leaderboard_service.sync_tile_count(user.id, outcome.owned_tile_count)
    return response


@router.post(
    "/walk",
    response_model=WalkReportResponse,
    summary="Report metres walked inside a hex",
)
def report_walk(
    payload: WalkReportRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> WalkReportResponse:
    """Credit ``payload.meters`` walked towards the hex containing (lat, lon).

    This is how the 20-metre claim rule stays server-owned, and it is now also
    how mana is earned. The client measures how far it has walked (GPS
    displacement) and reports the delta; the server validates it, credits the
    metres to a per-hex ledger *and* converts them to mana at
    ``MANA_PER_METER``. The response carries both, so the player sees distance
    progress toward their claim and the mana it just banked in one round trip.

    Rejected deltas (bigger than any real walk since the previous report, or a
    single report over the per-report ceiling) come back as 422 with the
    plausible figure, and leave the ledger, the balance and the baseline
    untouched -- a rejected report must not shrink the next one.
    """
    try:
        result = walk_service.record_walk(
            session,
            user_id=user.id,
            lat=payload.lat,
            lon=payload.lon,
            meters=payload.meters,
        )
    except walk_service.ImplausibleWalkReport as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from None
    except UserNotFound:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    required = get_settings().min_claim_distance_meters
    return WalkReportResponse(
        q=result.q,
        r=result.r,
        total=round(result.total, 1),
        required=required,
        remaining=max(0.0, round(required - result.total, 1)),
        mana=result.mana,
        mana_max=result.mana_max,
        walking_gained=result.walking_gained,
        passive_gained=result.passive_gained,
        total_mana_gained=result.walking_gained + result.passive_gained,
        daily_mana_earned=result.daily_mana_earned,
        daily_mana_remaining=result.daily_mana_remaining,
    )
