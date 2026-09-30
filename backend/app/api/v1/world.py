"""``GET /world`` -- the procedural hex plane around a coordinate.

Public (no auth required) so the map can render before sign-in, with the
caller's identity attached when a token happens to be present.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_optional_current_user
from app.core.config import get_settings
from app.core.constants import WORLD_VIEWPORT_MAX_RINGS, WORLD_VIEWPORT_RINGS
from app.db.session import get_db
from app.models.user import User
from app.schemas.world import WorldResponse
from app.services import walkability_service
from app.services.geo import Axial
from app.services.world_service import (
    ViewportOutsideLeash,
    ViewportTooLargeError,
    Walkability,
    WalkabilityResolver,
    build_viewport,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["world"])


def _walkability_resolver() -> WalkabilityResolver | None:
    """A resolver for this request, or ``None`` when walkability is disabled.

    Built per request so the setting is read live (tests toggle it) and the
    failure body for a dead Overpass is the *response flag*, not a 500. Fails
    open on purpose: a map that renders the whole grid during an outage is
    still a map, and claims likewise stay possible.
    """
    if not get_settings().walkability_enabled:
        return None

    def resolve(disc: list[Axial]) -> Walkability:
        try:
            return Walkability(
                walkable=walkability_service.walkability_for_disc(disc)
            )
        except walkability_service.WalkabilityUnavailable as exc:
            logger.warning(
                "walkability data unavailable; serving unfiltered viewport: %s", exc
            )
            return Walkability(walkable=None, degraded=True)

    return resolve


@router.get(
    "/world",
    summary="Hex tiles around a coordinate, with ownership",
    response_model=WorldResponse,
)
def get_world(
    lat: float = Query(
        ...,
        ge=-90.0,
        le=90.0,
        description="Centre latitude in degrees. Values beyond the Web Mercator "
        "limit are clamped rather than rejected.",
    ),
    lon: float = Query(
        ...,
        ge=-180.0,
        le=180.0,
        description="Centre longitude in degrees.",
    ),
    rings: int = Query(
        WORLD_VIEWPORT_RINGS,
        ge=0,
        le=WORLD_VIEWPORT_MAX_RINGS,
        description="Hexagon radius. Tiles returned is 3*rings*(rings+1)+1, so "
        "the default of 16 is 817 tiles.",
    ),
    viewer: User | None = Depends(get_optional_current_user),
    session: Session = Depends(get_db),
) -> WorldResponse:
    """Return every tile within ``rings`` of ``(lat, lon)``.

    When walkability is enabled the tiles are the walkable subset of the disc
    -- hexes that actually contain walkable ground -- and ``walkability_degraded``
    says so when that filtering had to be skipped.

    Unclaimed tiles come back with ``owner: null``; the client draws the grid
    itself and only needs the server to say who holds what. No rows are
    created by this call -- the plane is procedural and tiles materialise on
    first claim.
    """
    try:
        viewport = build_viewport(
            session,
            lat=lat,
            lon=lon,
            rings=rings,
            walkability_resolver=_walkability_resolver(),
            viewer_id=viewer.id if viewer else None,
        )
    except ViewportTooLargeError as exc:
        # Unreachable via HTTP because ``rings`` is bounded above, but the
        # service is public and must not be able to emit an unbounded response.
        #
        # HTTPException, not a bare ValueError: an uncaught ValueError is not
        # a validation failure to FastAPI, it is an unhandled exception, so
        # this would have become a 500. The 422 is the honest status for "your
        # parameter asked for too much", and a client that hits this branch
        # gets a readable body instead of an opaque Internal Server Error.
        logger.warning("rejected oversized viewport: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ViewportOutsideLeash as exc:
        # The centre is beyond a configured arena boundary. 422 for the same
        # reason as above: the parameter is out of the range this deployment
        # serves, and the body says how far out it is.
        logger.info("rejected viewport outside world leash: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    return WorldResponse.from_viewport(
        viewport, viewer_id=viewer.id if viewer else None
    )
