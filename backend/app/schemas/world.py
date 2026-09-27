"""Response contract for ``GET /world``.

Every tile carries its plane position. That is redundant with the ``(q, r)``
pair and could be recomputed by the client, but it is included on purpose:
the projection is the part of this system most likely to be got subtly wrong,
and shipping the server's own answer means the client never reimplements cube
rounding. At 817 tiles it costs a few kilobytes, and the response is gzipped.
"""

from __future__ import annotations

from dataclasses import asdict

from pydantic import BaseModel, ConfigDict, Field

from app.core.constants import HEX_SIZE_METERS
from app.services.world_service import WorldViewport


class TileOwner(BaseModel):
    """The public identity of whoever holds a tile.

    A nested object rather than a parallel ``owner_id``/``owner_name`` pair, so
    that a tile cannot end up with a name and an id that disagree, and so
    that phase 8 can add fields (owned tile count, college) without a second
    breaking change.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    display_name: str


class WorldTile(BaseModel):
    """One hex in the viewport."""

    q: int
    r: int
    world_x: float
    world_y: float
    """Centre of the hex in Web Mercator plane metres -- the engine's own
    projected unit, which is what a camera position or an on-plane distance
    calculation wants."""

    lat: float
    lon: float
    """Centre in WGS84, and what the map actually consumes.

    Mappls renders vector geometry from a GeoJSON source, and RFC 7946 fixes
    the coordinate order as ``[lon, lat]``. These are sent so the client can
    build a ``FeatureCollection`` without inverting the projection itself.
    """

    owner: TileOwner | None = None
    """``null`` means unclaimed, and is the normal case: the world is mostly
    empty and rows only exist once someone pays for them.

    Note what is *not* here: an email. This endpoint is public and unauthenticated
    by default, so anything it returns goes to anyone who loads a map.
    ``display_name`` is the only identifying field, which is exactly why
    ``users.display_name`` had to exist before Phase 4 could be useful."""


class WorldCentre(BaseModel):
    q: int
    r: int
    lat: float
    lon: float
    world_x: float
    world_y: float


class WorldBounds(BaseModel):
    """Exact extent of the returned tiles, in both coordinate spaces.

    Computed from real polygon vertices rather than ``rings * size``, so the
    client can size its map frame without clipping the outer ring.

    ``min_lon``/``max_lon`` can fall outside +/-180 only for a viewport more
    than one Mercator world away from the origin, which no player can reach.
    See ``test_the_reachable_plane_is_unbounded_east_west``.
    """

    min_lon: float
    max_lon: float
    min_lat: float
    max_lat: float
    min_world_x: float
    max_world_x: float
    min_world_y: float
    max_world_y: float


class WorldResponse(BaseModel):
    centre: WorldCentre
    rings: int
    tile_count: int
    owned_count: int
    """How many of the returned tiles are already claimed. Lets the client
    skip drawing a legend or warn about contested ground without a second
    request."""

    hex_size_meters: float = Field(default=HEX_SIZE_METERS)
    """Circumradius in *plane* metres. See ``HEX_SIZE_METERS`` for why this is
    not a ground distance outside the equator."""

    viewer_id: int | None = None
    """The authenticated user, when a token was supplied. The client compares
    this against each tile's ``owner.id`` to highlight its own territory.
    ``null`` for an anonymous request."""

    latitude_was_clamped: bool
    bounds: WorldBounds
    tiles: list[WorldTile]

    @classmethod
    def from_viewport(
        cls, viewport: WorldViewport, viewer_id: int | None = None
    ) -> "WorldResponse":
        return cls(
            centre=WorldCentre(
                q=viewport.centre_q,
                r=viewport.centre_r,
                lat=viewport.centre_lat,
                lon=viewport.centre_lon,
                world_x=viewport.centre_world_x,
                world_y=viewport.centre_world_y,
            ),
            rings=viewport.rings,
            tile_count=viewport.tile_count,
            owned_count=viewport.owned_count,
            hex_size_meters=viewport.hex_size_meters,
            viewer_id=viewer_id,
            latitude_was_clamped=viewport.latitude_was_clamped,
            # asdict, not vars: BoundingBox is a slots dataclass and so has no
            # __dict__ for vars() to read.
            bounds=WorldBounds(**asdict(viewport.bounds)),
            tiles=[
                WorldTile(
                    q=tile.q,
                    r=tile.r,
                    world_x=tile.world_x,
                    world_y=tile.world_y,
                    lat=tile.lat,
                    lon=tile.lon,
                    owner=(
                        TileOwner(id=tile.owner_id, display_name=tile.owner_display_name)
                        if tile.owner_id is not None
                        else None
                    ),
                )
                for tile in viewport.tiles
            ],
        )
