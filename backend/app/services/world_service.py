"""Assembling a viewport of the procedural hex plane for ``GET /world``.

The plane is unbounded and tiles materialise lazily, so this service answers
"which of these 817 coordinates already exist as rows, and who owns them".
It does not create rows and does not decide anything -- that is phase 5.

When a ``walkability_resolver`` is injected, the disc is additionally reduced
to the walkable subset before any row lookup or bounding box is computed, so
the response, its counts, and its bounds all describe only the hexes a player
can actually traverse. The resolver is injected rather than imported so this
module stays free of HTTP -- *what is walkable* is deliberately somebody
else's problem.

One deliberate efficiency choice: the lookup is a range scan on ``q`` and
``r`` followed by an exact hex-distance filter in Python, rather than an
``IN`` list of 817 tuples. The unique constraint ``uq_hex_tiles_q_r`` is a
btree on ``(q, r)``, so the scan is index-backed, and the overscan is only
33x33 = 1089 candidates for 817 results at R=16. An ``IN`` list would send
817 bound parameters per request and defeat the index.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import (
    COORDINATE_PRECISION,
    COORDINATE_PRECISION_DEGREES,
    HEX_SIZE_METERS,
    WORLD_MAX_LATITUDE_MERCATOR,
    WORLD_VIEWPORT_MAX_TILES,
)
from app.models.hex_tile import HexTile
from app.models.user import User
from app.services import walk_service
from app.services.geo import (
    Axial,
    BoundingBox,
    axial_to_lonlat,
    axial_to_world,
    hex_disc_slack,
    hex_distance,
    lonlat_to_axial,
    lonlat_to_world,
    tiles_bounding_box,
    tiles_in_radius,
    tile_count_in_radius,
)

# Coordinate precision now lives in app.core.constants, alongside every other
# tunable, so that the values cannot differ between the two services that emit
# coordinates.



class ViewportTooLargeError(ValueError):
    """Raised when a caller asks for more tiles than the response budget.

    A defence-in-depth check behind the ``rings`` query bound: the bound lives
    in the API schema where a client can see it, this lives in the service
    where it cannot be bypassed by a future caller.
    """


class ViewportOutsideLeash(ValueError):
    """Raised when a viewport centre is outside a configured world leash.

    The claim path is fenced by ``territory_service.check_world_leash``, and a
    view that was not fenced would make that setting half a feature: a player
    could map the entire plane, see every territory and every owner's name, and
    only be prevented from claiming. Refusing the centre is consistent with
    refusing the claim, and unlike clamping it does not quietly return a
    viewport the caller did not ask for.
    """


@dataclass(frozen=True, slots=True)
class Walkability:
    """The decision a resolver made about a disc: filter, or degrade.

    ``walkable=None`` with ``degraded=False`` is "no filtering happened" (the
    feature is off); ``walkable=None`` with ``degraded=True`` is "filtering
    was attempted but the data source failed, so the full plane is served and
    the response flags it". A non-``None`` ``walkable`` is a real filter.
    """
    walkable: set[tuple[int, int]] | None
    degraded: bool = False


#: Turns an exact disc into :class:`Walkability`. Injected from the API layer,
#: where Overpass and its failure mode are configured.
WalkabilityResolver = Callable[[list[Axial]], Walkability]


@dataclass(frozen=True, slots=True)
class ViewportTile:
    q: int
    r: int
    world_x: float
    world_y: float
    lat: float
    lon: float
    """Centre in WGS84 as well as in the Web Mercator plane.

    Both are sent because they answer different questions. ``lat``/``lon`` is
    what a GeoJSON source consumes -- RFC 7946 fixes the order as
    ``[lon, lat]`` -- and Mappls, like Mapbox and MapLibre, takes its vector
    geometry as GeoJSON. ``world_x``/``world_y`` stays because it is the
    engine's own projected unit and is what a camera or a distance calculation
    wants.

    Shipping the server's own lon/lat is the whole point of the arrangement:
    the alternative is the client inverting the projection, which is the same
    class of duplicated-geometry bug as reimplementing cube rounding.
    """
    owner_id: int | None
    owner_display_name: str | None
    """Always set when ``owner_id`` is. Kept as two fields rather than a nested
    object so this stays a flat slots dataclass that the response can be built
    from without allocating an intermediate model per tile."""
    walk_meters: float = 0.0
    """How far the *viewer* has walked inside this hex. Zero for every other
    player and for anonymous viewers -- nobody else's progress is this module's
    business to expose."""


@dataclass(frozen=True, slots=True)
class WorldViewport:
    centre_q: int
    centre_r: int
    centre_lat: float
    centre_lon: float
    centre_world_x: float
    centre_world_y: float
    rings: int
    tile_count: int
    owned_count: int
    hex_size_meters: float
    bounds: BoundingBox
    tiles: tuple[ViewportTile, ...]
    latitude_was_clamped: bool
    """True when the requested latitude exceeded the Web Mercator limit and the
    centre had to be pulled back to it. Surfaced rather than hidden so a client
    that somehow reaches the dead zone can tell the difference between "this is
    where you are" and "this is as close as the projection gets"."""
    walkability_degraded: bool = False
    """True only when walkability was enabled but the data source could not be
    reached, so the viewport was served unfiltered. The client can say "the
    whole grid is showing because the walkability check is down" instead of
    silently believing every open field is claimable ground."""


def clamp_rings_to_leash(
    *,
    centre: Axial,
    rings: int,
    hex_size_meters: float = HEX_SIZE_METERS,
) -> int:
    """Bound a viewport to a configured world leash.

    Inert when ``world_max_radius_from_origin`` is unset, which is the default.
    When it is set, this makes the *view* obey the same arena the *claim* rule
    does. Without it the setting fences claims while leaving the map open, and
    "fenced arena" turns out to mean a player can see every territory and every
    owner's name and simply cannot touch any of it.

    A centre inside the leash keeps as much of the requested radius as fits; a
    centre outside is refused, because there is no honest viewport to return.
    """
    settings = get_settings()
    max_radius = settings.world_max_radius_from_origin
    if max_radius is None:
        return rings

    origin = lonlat_to_axial(
        settings.world_origin_lat, settings.world_origin_lon, hex_size_meters
    )
    slack = hex_disc_slack(origin, centre, max_radius)
    if slack < 0:
        raise ViewportOutsideLeash(
            f"centre is {hex_distance(origin, centre)} rings from the world "
            f"origin, outside the configured leash of {max_radius}"
        )
    return min(rings, slack)


def load_owners(
    session: Session,
    centre: Axial,
    tiles: list[Axial],
    rings: int,
) -> dict[Axial, tuple[int, str]]:
    """Map every *claimed* coordinate in the viewport to ``(owner_id, name)``.

    Unclaimed coordinates are simply absent from the result. Keeping the
    unclaimed ones out of the dict means the caller distinguishes "nobody owns
    this" from "this does not exist", which is not a distinction the game
    makes anywhere else -- there is no unowned row in ``hex_tiles`` to
    confuse it with.

    ``centre`` is passed explicitly rather than recovered from ``tiles``. The
    list is in column-major order, so ``tiles[0]`` is the *southwest corner* of
    the viewport; filtering hex distance against that would keep a disc
    shifted one corner out of position and silently drop a real wedge of
    claimed tiles.
    """
    if not tiles:
        return {}

    q_values = [q for q, _ in tiles]
    r_values = [r for _, r in tiles]

    # Joined to users because the map has to label each territory with a name.
    # An inner join is deliberate and load-bearing: a row with a non-null
    # owner_id whose user has vanished is a data-integrity bug, and silently
    # rendering it as unclaimed would hide that bug behind a plausible-looking
    # empty hex.
    statement = (
        select(HexTile.q, HexTile.r, User.id, User.display_name)
        .join(User, User.id == HexTile.owner_id)
        .where(
            HexTile.q.between(min(q_values), max(q_values)),
            HexTile.r.between(min(r_values), max(r_values)),
        )
    )

    owners: dict[Axial, tuple[int, str]] = {}
    for q, r, owner_id, display_name in session.execute(statement):
        # The range scan over-collects the corners of the axial bounding box.
        # Dropping them here is a *bound on this dict*, not the reason the
        # response is correct: build_viewport iterates the hex disc and probes
        # this lookup, so an extra key would never be read. Kept because it
        # costs one comparison per row, keeps the dict to 817 entries rather
        # than 1089, and means a future refactor that iterated this dict
        # instead would not silently start leaking corners.
        if hex_distance(centre, (q, r)) <= rings:
            owners[(q, r)] = (owner_id, display_name)
    return owners


def build_viewport(
    session: Session,
    *,
    lat: float,
    lon: float,
    rings: int,
    hex_size_meters: float = HEX_SIZE_METERS,
    walkability_resolver: WalkabilityResolver | None = None,
    viewer_id: int | None = None,
) -> WorldViewport:
    """The full ``GET /world`` answer for a centre coordinate and radius."""
    if tile_count_in_radius(rings) > WORLD_VIEWPORT_MAX_TILES:
        raise ViewportTooLargeError(
            f"rings={rings} would return {tile_count_in_radius(rings)} tiles, "
            f"over the {WORLD_VIEWPORT_MAX_TILES} budget"
        )

    clamped = abs(lat) > WORLD_MAX_LATITUDE_MERCATOR
    centre_x, centre_y = lonlat_to_world(lon, lat)
    centre_q, centre_r = lonlat_to_axial(lat, lon, hex_size_meters)

    rings = clamp_rings_to_leash(
        centre=(centre_q, centre_r), rings=rings, hex_size_meters=hex_size_meters
    )

    coordinates = list(tiles_in_radius(centre_q, centre_r, rings))

    # The resolver is handed the *post-clamp, exact* disc, so a viewport that
    # the leash shrank is filtered on what the response actually covers.
    walkability_degraded = False
    if walkability_resolver is not None:
        resolution = walkability_resolver(coordinates)
        walkability_degraded = resolution.degraded
        if resolution.walkable is not None:
            coordinates = [c for c in coordinates if c in resolution.walkable]

    owners = load_owners(session, (centre_q, centre_r), coordinates, rings)

    # The viewer's own walk progress across the same disc, when there is a
    # viewer. Same range-scan shape as load_owners, so the extra cost is one
    # index-backed query when the caller is authenticated and zero otherwise.
    walk_progress = (
        walk_service.progress_map(session, viewer_id, coordinates)
        if viewer_id is not None
        else {}
    )

    built: list[ViewportTile] = []
    for q, r in coordinates:
        x, y = axial_to_world(q, r, hex_size_meters)
        # axial_to_lonlat returns (lon, lat). Unpacking this the other way
        # round silently transposes every tile, which is exactly the bug that
        # was already hit once on the viewport centre.
        tile_lon, tile_lat = axial_to_lonlat(q, r, hex_size_meters)
        owner = owners.get((q, r))
        built.append(
            ViewportTile(
                q=q,
                r=r,
                world_x=round(x, COORDINATE_PRECISION),
                world_y=round(y, COORDINATE_PRECISION),
                lat=round(tile_lat, COORDINATE_PRECISION_DEGREES),
                lon=round(tile_lon, COORDINATE_PRECISION_DEGREES),
                owner_id=owner[0] if owner else None,
                owner_display_name=owner[1] if owner else None,
                walk_meters=walk_progress.get((q, r), 0.0),
            )
        )
    tiles = tuple(built)

    # axial_to_lonlat returns (lon, lat). Unpacking this the other way round
    # silently transposes the centre echo, which is exactly the kind of bug a
    # test that only checks "there is a centre" will never catch.
    safe_lon, safe_lat = axial_to_lonlat(centre_q, centre_r, hex_size_meters)

    return WorldViewport(
        centre_q=centre_q,
        centre_r=centre_r,
        centre_lat=round(safe_lat, COORDINATE_PRECISION_DEGREES),
        centre_lon=round(safe_lon, COORDINATE_PRECISION_DEGREES),
        centre_world_x=round(centre_x, COORDINATE_PRECISION),
        centre_world_y=round(centre_y, COORDINATE_PRECISION),
        rings=rings,
        tile_count=len(tiles),
        owned_count=sum(1 for tile in tiles if tile.owner_id is not None),
        hex_size_meters=hex_size_meters,
        bounds=tiles_bounding_box(coordinates, hex_size_meters),
        tiles=tiles,
        latitude_was_clamped=clamped,
        walkability_degraded=walkability_degraded,
    )
