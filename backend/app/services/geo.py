"""Pure hex geometry and the location -> axial projection.

This module holds no database, no HTTP, and no imports from ``app.models`` or
``app.api``. That is deliberate: it is the one piece of the system whose
correctness must be provable by arithmetic rather than by integration, and
keeping it dependency-free means every rule below is testable without a
Postgres round trip.

The pipeline is::

    lat/lon  --(spherical Web Mercator)-->  plane metres
             --(pointy-top axial)----------->  fractional axial
             --(cube rounding)-------------->  integer axial (q, r)

Why cube rounding and not a nearest-integer on each axis independently: axial
coordinates are a redundant 2-D encoding of a 3-D point on a cube
(``x + y + z = 0``), so rounding ``q`` and ``r`` separately can produce a pair
that does not correspond to any hex at all. Rounding all three and repairing
the largest error guarantees the result is a real axial coordinate.

Pointy-top orientation is used throughout (matching MapLibre/Mapbox screen
conventions and Red Blob's reference implementation). The orientation is not
recorded in the database -- it is implied by :data:`HEX_SIZE_METERS` and this
module, so changing it later would invalidate every stored ``(q, r)``.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass

from app.core.constants import (
    HEX_NEIGHBOR_OFFSETS,
    HEX_SIZE_METERS,
    WEB_MERCATOR_RADIUS_METERS,
    WORLD_MAX_LATITUDE_MERCATOR,
)

SQRT3 = math.sqrt(3.0)
_DEG_TO_RAD = math.pi / 180.0

Axial = tuple[int, int]
FractionalAxial = tuple[float, float]
WorldPoint = tuple[float, float]


# ---------------------------------------------------------------------------
# Web Mercator (EPSG:3857)
# ---------------------------------------------------------------------------
# The projection MapLibre/Mapbox render in, so "plane metres" here are the
# same units the client will use to place polygons. Using anything else would
# mean the server and client disagree about where a tile is.


def clamp_latitude(lat: float) -> float:
    """Clamp to the latitude Web Mercator can represent.

    Mercator diverges at the poles: ``tan(pi/4 + pi/2)`` is infinite, so a
    player at 89 degrees north would otherwise produce an infinite ``y`` and
    eventually an OverflowError. Clamping at the standard 85.05112878
    (where the projection is square) is what every web map does, and it
    degrades gracefully: a GPS reading a few degrees off the pole becomes a
    valid, if imprecise, position instead of a 500.
    """
    return max(-WORLD_MAX_LATITUDE_MERCATOR, min(WORLD_MAX_LATITUDE_MERCATOR, lat))


def normalise_longitude(lon: float) -> float:
    """Fold any longitude into ``[-180, 180)``.

    A GPS fix of 190 or -190 degrees is well-formed input from a client that
    has simply walked past the antimeridian, and both mean the same meridian.
    """
    return (lon + 180.0) % 360.0 - 180.0


def lonlat_to_world(lon: float, lat: float) -> WorldPoint:
    """Spherical Web Mercator: degrees -> plane metres."""
    x = WEB_MERCATOR_RADIUS_METERS * normalise_longitude(lon) * _DEG_TO_RAD
    safe_lat = clamp_latitude(lat) * _DEG_TO_RAD
    y = WEB_MERCATOR_RADIUS_METERS * math.log(math.tan(math.pi / 4.0 + safe_lat / 2.0))
    return x, y


def world_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """Inverse of :func:`lonlat_to_world`; returns ``(lon, lat)`` in degrees."""
    lon = (x / WEB_MERCATOR_RADIUS_METERS) / _DEG_TO_RAD
    lat = (2.0 * math.atan(math.exp(y / WEB_MERCATOR_RADIUS_METERS)) - math.pi / 2.0) / _DEG_TO_RAD
    return lon, lat


def ground_distance_scale(lat: float) -> float:
    """How much Web Mercator stretches lengths at ``lat``: ``cos(lat)``.

    A projected length ``p`` corresponds to ``p * cos(lat)`` on the ground,
    because the projection's local scale factor is ``1 / cos(lat)``.

    This is the honest caveat on :data:`HEX_SIZE_METERS`. A "40 m" hex is
    40 m on the ground *at the equator*; at 40 degrees north the same hex
    spans about 53 m, and at 60 degrees north about 35 m. See
    ``HEX_SIZE_METERS`` for why the projected reading is the deliberate
    choice rather than an oversight.
    """
    return math.cos(clamp_latitude(lat) * _DEG_TO_RAD)


# ---------------------------------------------------------------------------
# Axial <-> plane
# ---------------------------------------------------------------------------


def axial_to_world(
    q: float, r: float, size: float = HEX_SIZE_METERS
) -> WorldPoint:
    """Fractional axial -> plane metres, pointy-top.

    The columns come from tiling the plane with equilateral triangles of side
    ``size``; the rows are spaced ``1.5 * size`` apart, which is why the
    inverse is not symmetric with this.
    """
    x = size * (SQRT3 * q + (SQRT3 / 2.0) * r)
    y = size * (1.5 * r)
    return x, y


def world_to_axial(
    x: float, y: float, size: float = HEX_SIZE_METERS
) -> FractionalAxial:
    """Plane metres -> fractional axial, pointy-top. Exact inverse of
    :func:`axial_to_world` before rounding."""
    q = ((SQRT3 / 3.0) * x - (1.0 / 3.0) * y) / size
    r = ((2.0 / 3.0) * y) / size
    return q, r


def cube_round(q_fraction: float, r_fraction: float) -> Axial:
    """Snap a fractional axial coordinate to the nearest real hex.

    ``floor(x + 0.5)`` rather than :func:`round` on purpose: Python rounds
    halves to even (``round(0.5) == 0``, ``round(1.5) == 2``), which is a
    surprise in code ported from the Java/C reference implementations this
    follows, all of which use ``floor(x + 0.5)``.

    After rounding, at most one axis is inconsistent with ``x + y + z = 0``.
    Dropping the most-wrong axis and recomputing it from the other two is what
    makes the output a valid axial coordinate rather than merely a nearby
    integer pair.
    """
    s_fraction = -q_fraction - r_fraction

    q = math.floor(q_fraction + 0.5)
    r = math.floor(r_fraction + 0.5)
    s = math.floor(s_fraction + 0.5)

    q_error = abs(q - q_fraction)
    r_error = abs(r - r_fraction)
    s_error = abs(s - s_fraction)

    if q_error > r_error and q_error > s_error:
        q = -r - s
    elif r_error > s_error:
        r = -q - s

    return q, r


def lonlat_to_axial(
    lat: float, lon: float, size: float = HEX_SIZE_METERS
) -> Axial:
    """The full client-facing projection: a GPS fix to the tile containing it."""
    x, y = lonlat_to_world(lon, lat)
    q_fraction, r_fraction = world_to_axial(x, y, size)
    return cube_round(q_fraction, r_fraction)


def axial_to_lonlat(
    q: int, r: int, size: float = HEX_SIZE_METERS
) -> tuple[float, float]:
    """Tile centre back to ``(lon, lat)`` -- the inverse used for naming and
    for the viewport centre echo."""
    x, y = axial_to_world(q, r, size)
    return world_to_lonlat(x, y)


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------


def cube(q: int, r: int) -> tuple[int, int, int]:
    """Axial -> cube coordinates."""
    return q, r, -q - r


def hex_distance(a: Axial, b: Axial) -> int:
    """Hex distance: the number of steps between two tiles.

    On the cube this is half the sum of the absolute differences, which -- as
    a max over the three axes -- equals the same thing. Used in preference to
    a Euclidean distance on the plane, because "adjacent" must be exact: two
    tiles that are 1.4 steps apart in a straight line are still two steps
    apart on the grid.
    """
    aq, ar, as_ = cube(*a)
    bq, br, bs = cube(*b)
    return max(abs(aq - bq), abs(ar - br), abs(as_ - bs))


def neighbors(q: int, r: int) -> tuple[Axial, ...]:
    """The six tiles sharing an edge with ``(q, r)``, clockwise from ``(+1, 0)``."""
    return tuple((q + dq, r + dr) for dq, dr in HEX_NEIGHBOR_OFFSETS)


def hex_disc_slack(origin: Axial, centre: Axial, max_radius: int) -> int:
    """How many rings a disc about ``centre`` may have and stay inside the leash.

    ``hex_distance`` is a metric, so a disc of radius ``r`` about ``centre``
    reaches no further than ``r + hex_distance(origin, centre)`` from the
    origin. Subtracting the centre's own distance from the leash radius is
    therefore *exact* rather than an approximation: every tile the disc contains
    is inside the leash. It is the same triangle-inequality arithmetic that
    decides whether a claim is out of bounds.

    Returns a negative number when the centre is itself outside the leash, which
    the caller reports rather than clamps -- silently answering a request far
    outside the arena with a one-tile viewport would be a confusing reply to an
    honest question.
    """
    return max_radius - hex_distance(origin, centre)


def are_adjacent(a: Axial, b: Axial) -> bool:
    """True when the two tiles share an edge.

    The one place territory adjacency is decided, in phase 5. Deliberately
    ``hex_distance == 1`` and nothing looser: a claim three rings away must
    be rejected, not approximated.
    """
    return hex_distance(a, b) == 1


def tile_count_in_radius(rings: int) -> int:
    """Exactly ``3 * rings * (rings + 1) + 1`` tiles in a hexagon of radius
    ``rings``. The closed form is used to enforce the response-size cap
    without enumerating."""
    if rings < 0:
        raise ValueError("rings must be non-negative")
    return 3 * rings * (rings + 1) + 1


def max_rings_for_tile_count(budget: int) -> int:
    """Largest radius whose hexagon still fits in ``budget`` tiles."""
    rings = 0
    while tile_count_in_radius(rings + 1) <= budget:
        rings += 1
    return rings


def tiles_in_radius(
    center_q: int, center_r: int, rings: int
) -> Iterator[Axial]:
    """Every tile within ``rings`` of the centre, in a stable order.

    Implemented as a scan over the ``dq`` columns rather than
    ``(2r+1)^2`` nested loops with a distance filter: the inner bounds below
    are exactly the tiles in range, so this visits each tile once and never
    tests and discards a candidate. The column-major order is stable, which
    makes viewport responses byte-comparable between calls -- useful for
    caching and for tests.
    """
    if rings < 0:
        raise ValueError("rings must be non-negative")
    for dq in range(-rings, rings + 1):
        # In axial space the constraint |dq + dr| <= rings splits into
        # -rings - dq <= dr <= rings - dq.
        low = max(-rings, -dq - rings)
        high = min(rings, -dq + rings)
        for dr in range(low, high + 1):
            yield center_q + dq, center_r + dr


def hex_corners(
    centre_x: float, centre_y: float, size: float = HEX_SIZE_METERS
) -> tuple[WorldPoint, ...]:
    """The six polygon vertices of a pointy-top hex, clockwise from the top.

    Angles start at -30 degrees so the first vertex is the upper-right point
    of a pointy-top hex.
    """
    return tuple(
        (
            centre_x + size * math.cos((60 * index - 30) * _DEG_TO_RAD),
            centre_y + size * math.sin((60 * index - 30) * _DEG_TO_RAD),
        )
        for index in range(6)
    )


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """Axis-aligned extent in both plane metres and degrees."""

    min_world_x: float
    max_world_x: float
    min_world_y: float
    max_world_y: float
    min_lon: float
    max_lon: float
    min_lat: float
    max_lat: float


def tiles_bounding_box(
    tiles: list[Axial], size: float = HEX_SIZE_METERS
) -> BoundingBox:
    """Exact bounding box of a set of hexes.

    Computed from the real polygon vertices of every tile rather than from
    ``size * rings`` multiples, because a hex disc of radius R is wider than
    ``2 * R * size`` in some directions and narrower in others -- an
    approximation here would let the client request a viewport that clips its
    own outer ring. Cost is 6 float pairs per tile, which is negligible at the
    817-tile scale and removes a whole class of off-by-one guesswork.
    """
    xs: list[float] = []
    ys: list[float] = []
    for q, r in tiles:
        centre_x, centre_y = axial_to_world(q, r, size)
        for corner_x, corner_y in hex_corners(centre_x, centre_y, size):
            xs.append(corner_x)
            ys.append(corner_y)

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    min_lon, min_lat = world_to_lonlat(min_x, min_y)
    max_lon, max_lat = world_to_lonlat(max_x, max_y)

    # Web Mercator's y grows northward, so min_y is the southern edge.
    return BoundingBox(
        min_world_x=min_x,
        max_world_x=max_x,
        min_world_y=min_y,
        max_world_y=max_y,
        min_lon=min_lon,
        max_lon=max_lon,
        min_lat=min_lat,
        max_lat=max_lat,
    )
