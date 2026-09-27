"""``GET /world`` -- the procedural hex plane around a coordinate.

Public (no auth required) so the map can render before sign-in, with the
caller's identity attached when a token happens to be present.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_optional_current_user
from app.core.constants import WORLD_VIEWPORT_MAX_RINGS, WORLD_VIEWPORT_RINGS
from app.db.session import get_db
from app.models.user import User
from app.schemas.world import WorldResponse
from app.services.world_service import (
    ViewportOutsideLeash,
    ViewportTooLargeError,
    build_viewport,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["world"])


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

    Unclaimed tiles come back with ``owner: null``; the client draws the grid
    itself and only needs the server to say who holds what. No rows are
    created by this call -- the plane is procedural and tiles materialise on
    first claim.
    """
    try:
        viewport = build_viewport(session, lat=lat, lon=lon, rings=rings)
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
