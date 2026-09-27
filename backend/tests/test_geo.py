"""Geometry and projection tests.

These are pure-arithmetic tests with no database and no HTTP, which is the
point of keeping ``app.services.geo`` dependency-free: the hex maths is the
part of the system where a silent error produces a plausible-looking map with
tiles in the wrong places, so it is verified by properties rather than by a
handful of golden values.
"""

from __future__ import annotations

import math

import pytest

from app.core.constants import (
    HEX_NEIGHBOR_OFFSETS,
    HEX_SIZE_METERS,
    WEB_MERCATOR_RADIUS_METERS,
    WORLD_MAX_LATITUDE_MERCATOR,
    WORLD_VIEWPORT_MAX_RINGS,
    WORLD_VIEWPORT_MAX_TILES,
    WORLD_VIEWPORT_RINGS,
)
from app.services.geo import (
    axial_to_lonlat,
    axial_to_world,
    are_adjacent,
    clamp_latitude,
    cube,
    cube_round,
    ground_distance_scale,
    hex_corners,
    hex_distance,
    lonlat_to_axial,
    lonlat_to_world,
    max_rings_for_tile_count,
    neighbors,
    normalise_longitude,
    tiles_bounding_box,
    tiles_in_radius,
    tile_count_in_radius,
    world_to_axial,
    world_to_lonlat,
)

SQRT3 = math.sqrt(3.0)

#: A spread of coordinates including the origin, both axes' extremes, and
#: values far enough out to catch an off-by-one in a sign.
SAMPLE_TILES = [
    (0, 0),
    (1, 0),
    (0, 1),
    (-1, 0),
    (0, -1),
    (1, -1),
    (-1, 1),
    (-16, 16),
    (16, -16),
    (-37, 5),
    (250, -180),
    (-400, 91),
]


# ---------------------------------------------------------------------------
# Viewport enumeration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rings", [0, 1, 2, 3, 7, 16, 19])
def test_enumeration_count_matches_the_closed_form(rings: int) -> None:
    """3R(R+1)+1 is the spec's "roughly 800"; enumeration must agree with it
    at every radius, not just the default."""
    enumerated = list(tiles_in_radius(0, 0, rings))
    assert len(enumerated) == tile_count_in_radius(rings)
    assert len(enumerated) == len(set(enumerated)), "a tile was emitted twice"


def test_default_viewport_is_817_tiles() -> None:
    """The spec's "roughly 800 tiles" figure, pinned."""
    assert tile_count_in_radius(WORLD_VIEWPORT_RINGS) == 817
    assert len(list(tiles_in_radius(0, 0, WORLD_VIEWPORT_RINGS))) == 817


def test_enumeration_returns_exactly_the_tiles_within_the_radius() -> None:
    rings = 8
    enumerated = set(tiles_in_radius(3, -2, rings))

    expected = {
        (3 + dq, -2 + dr)
        for dq in range(-rings - 2, rings + 3)
        for dr in range(-rings - 2, rings + 3)
        if hex_distance((3, -2), (3 + dq, -2 + dr)) <= rings
    }
    assert enumerated == expected


def test_enumeration_is_centred_on_the_requested_coordinate() -> None:
    """Catches the bug where the inner bounds are derived from the wrong
    origin, which would return a viewport shifted off the player."""
    for centre in [(0, 0), (5, 5), (-40, 17)]:
        tiles = list(tiles_in_radius(*centre, 6))
        assert all(hex_distance(centre, t) <= 6 for t in tiles)
        # The centre itself is always present.
        assert centre in tiles
        # And the disc is symmetric about it.
        assert len(tiles) == tile_count_in_radius(6)


def test_enumeration_order_is_stable() -> None:
    """Stable ordering makes viewport responses comparable between calls."""
    assert list(tiles_in_radius(2, 3, 5)) == list(tiles_in_radius(2, 3, 5))


def test_rings_zero_is_just_the_centre() -> None:
    assert list(tiles_in_radius(4, 9, 0)) == [(4, 9)]


def test_negative_rings_is_rejected() -> None:
    with pytest.raises(ValueError):
        list(tiles_in_radius(0, 0, -1))
    with pytest.raises(ValueError):
        tile_count_in_radius(-1)


def test_max_rings_constant_agrees_with_the_tile_budget() -> None:
    """The published API bound and the enforced budget must not drift apart."""
    assert max_rings_for_tile_count(WORLD_VIEWPORT_MAX_TILES) == (
        WORLD_VIEWPORT_MAX_RINGS
    )
    assert tile_count_in_radius(WORLD_VIEWPORT_MAX_RINGS) <= WORLD_VIEWPORT_MAX_TILES
    assert (
        tile_count_in_radius(WORLD_VIEWPORT_MAX_RINGS + 1)
        > WORLD_VIEWPORT_MAX_TILES
    )


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tile", SAMPLE_TILES)
def test_cube_coordinates_sum_to_zero(tile: tuple[int, int]) -> None:
    q, r, s = cube(*tile)
    assert q + r + s == 0


@pytest.mark.parametrize("tile", SAMPLE_TILES)
def test_hex_distance_matches_the_max_axis_form(tile: tuple[int, int]) -> None:
    """max(|dq|,|dr|,|dq+dr|) and half the cube sum are the same function;
    if they ever disagree, one of them is wrong and adjacency breaks."""
    for dq, dr in [(1, 0), (0, 1), (1, -1), (-1, 0), (0, -1), (-1, 1), (3, -2)]:
        other = (tile[0] + dq, tile[1] + dr)
        half_sum = sum(abs(a - b) for a, b in zip(cube(*tile), cube(*other))) // 2
        assert hex_distance(tile, other) == max(abs(dq), abs(dr), abs(dq + dr))
        assert hex_distance(tile, other) == half_sum


@pytest.mark.parametrize("tile", SAMPLE_TILES)
def test_distance_is_symmetric_and_reflexive(tile: tuple[int, int]) -> None:
    assert hex_distance(tile, tile) == 0
    for dq, dr in HEX_NEIGHBOR_OFFSETS:
        other = (tile[0] + dq, tile[1] + dr)
        assert hex_distance(tile, other) == hex_distance(other, tile) == 1
        assert hex_distance(tile, other) == 1


def test_neighbors_are_exactly_the_six_adjacent_tiles() -> None:
    for tile in SAMPLE_TILES:
        found = neighbors(*tile)
        assert len(found) == 6
        assert len(set(found)) == 6, "duplicate neighbour"
        assert all(are_adjacent(tile, n) for n in found)
        # Nothing adjacent was missed: the adjacency offsets are the
        # definition, so this is really pinning the constant.
        assert set(found) == {
            (tile[0] + dq, tile[1] + dr) for dq, dr in HEX_NEIGHBOR_OFFSETS
        }


def test_adjacency_is_exact_and_not_approximate() -> None:
    """Two rings apart is not adjacent, however close it looks on screen."""
    assert are_adjacent((0, 0), (1, 0))
    assert are_adjacent((0, 0), (0, -1))
    assert not are_adjacent((0, 0), (0, 0))
    assert not are_adjacent((0, 0), (2, 0))
    assert not are_adjacent((0, 0), (2, -1))
    assert not are_adjacent((0, 0), (-2, 1))


def test_interior_tiles_have_all_six_neighbours_inside_the_viewport() -> None:
    """A hole or a doubled edge in the grid would show up as a tile whose
    neighbour count differs, so count them all.

    Boundary tiles legitimately have fewer: one in the middle of a straight
    edge of the disc has four in-grid neighbours, and only the six *corner*
    tiles of the disc have three. Getting this wrong is easy -- an earlier
    version of this test assumed every ring tile had three and was wrong
    about 18 of the 24.
    """
    radius = 4
    corners = 0
    edge_tiles = 0
    interior_tiles = 0

    for q, r in tiles_in_radius(0, 0, radius):
        distance = hex_distance((0, 0), (q, r))
        inside = [
            n for n in neighbors(q, r) if hex_distance((0, 0), n) <= radius
        ]

        if distance < radius:
            assert len(inside) == 6, f"interior tile {(q, r)} has {len(inside)}"
            interior_tiles += 1
        elif len(inside) == 3:
            corners += 1
        elif len(inside) == 4:
            edge_tiles += 1
        else:
            raise AssertionError(
                f"ring tile {(q, r)} has {len(inside)} in-grid neighbours; "
                "a boundary tile can only have 3 or 4"
            )

    # The ring holds exactly 6*radius tiles, of which exactly six are the
    # corners of the disc. This is the shape check that would catch a
    # distorted viewport.
    assert corners == 6
    assert edge_tiles == 6 * radius - 6
    assert corners + edge_tiles == 6 * radius
    assert interior_tiles == tile_count_in_radius(radius) - 6 * radius


def test_the_six_adjacent_centres_are_one_spacing_apart() -> None:
    """Neighbour centre-to-centre distance must be sqrt(3)*S, which is what
    makes a pointy-top hex tile without gaps."""
    centre = axial_to_world(0, 0)
    spacing = math.sqrt(
        (axial_to_world(*neighbors(0, 0)[0])[0] - centre[0]) ** 2
        + (axial_to_world(*neighbors(0, 0)[0])[1] - centre[1]) ** 2
    )
    assert spacing == pytest.approx(SQRT3 * HEX_SIZE_METERS)


def test_axial_grid_has_no_gaps_between_neighbouring_hexes() -> None:
    """The circumradius-to-centre-spacing ratio is what guarantees the hexagons
    tile the plane. If size and spacing ever disagree, the overlay develops
    slivers and overlaps on the client.

    Horizontal spacing between (0,0) and (1,0) must equal the width of a
    pointy-top hex, sqrt(3)*S, and the row pitch must be 1.5*S.
    """
    dx = axial_to_world(1, 0)[0] - axial_to_world(0, 0)[0]
    assert dx == pytest.approx(SQRT3 * HEX_SIZE_METERS)
    dy = axial_to_world(0, 1)[1] - axial_to_world(0, 0)[1]
    assert dy == pytest.approx(1.5 * HEX_SIZE_METERS)


def test_hex_corners_are_on_the_circumcircle() -> None:
    cx, cy = axial_to_world(3, -2)
    corners = hex_corners(cx, cy)
    assert len(corners) == 6
    for corner_x, corner_y in corners:
        assert math.hypot(corner_x - cx, corner_y - cy) == pytest.approx(
            HEX_SIZE_METERS
        )
    # A pointy-top hex has a vertex straight up.
    top = min(corners, key=lambda c: c[1])
    assert top[0] == pytest.approx(cx)


# ---------------------------------------------------------------------------
# Axial <-> plane
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tile", SAMPLE_TILES)
def test_axial_world_round_trip(tile: tuple[int, int]) -> None:
    x, y = axial_to_world(*tile)
    assert world_to_axial(x, y) == pytest.approx((float(tile[0]), float(tile[1])))


@pytest.mark.parametrize("tile", SAMPLE_TILES)
def test_lonlat_round_trip_returns_the_same_tile(tile: tuple[int, int]) -> None:
    """The single most important property in this module: the tile a player is
    standing in must be the tile the client draws under them. An off-by-one
    here is invisible in a unit test that only checks the maths, and obvious
    to any player whose claim lands one hex away."""
    lon, lat = axial_to_lonlat(*tile)
    assert lonlat_to_axial(lat, lon) == tile


def test_lonlat_to_axial_of_zero_is_the_origin() -> None:
    assert lonlat_to_axial(0.0, 0.0) == (0, 0)
    assert axial_to_lonlat(0, 0) == pytest.approx((0.0, 0.0), abs=1e-9)


# ---------------------------------------------------------------------------
# Cube rounding
# ---------------------------------------------------------------------------


def test_cube_round_leaves_exact_coordinates_alone() -> None:
    for tile in SAMPLE_TILES:
        assert cube_round(float(tile[0]), float(tile[1])) == tile


def test_cube_round_always_returns_a_valid_axial_coordinate() -> None:
    """The output must satisfy x+y+z=0, or it is not a hex that exists."""
    for q in range(-12, 13):
        for r in range(-12, 13):
            q_rounded, r_rounded = cube_round(q * 0.37, r * 0.41)
            s_rounded = cube(q_rounded, r_rounded)[2]
            assert q_rounded + r_rounded + s_rounded == 0


def test_cube_round_picks_a_nearest_hex() -> None:
    """Never further than the circumradius in plane space.

    The bound is measured in *plane* metres, not in fractional axial units:
    the q and r axes are not orthogonal unit vectors, so a Euclidean distance
    taken directly on (q, r) overstates the real error by up to a factor of
    two and would reject correct results.
    """
    for q in range(-6, 7):
        for r in range(-6, 7):
            qf, rf = q + 0.49, r - 0.48
            rounded = cube_round(qf, rf)
            sample_x, sample_y = axial_to_world(qf, rf)
            centre_x, centre_y = axial_to_world(*rounded)
            offset = math.hypot(sample_x - centre_x, sample_y - centre_y)
            assert offset <= HEX_SIZE_METERS + 1e-9, (
                f"sample ({qf}, {rf}) rounded to {rounded} is {offset:.3f} m "
                f"away, past the {HEX_SIZE_METERS} m circumradius"
            )


def test_cube_round_uses_floor_half_up_not_bankers_rounding() -> None:
    """Documented deviation from :func:`round`, matching the Java/C reference
    implementations. Python's round() would give 0 and 2 here."""
    assert cube_round(0.5, -0.5) == (1, -1)
    assert cube_round(1.5, -1.5) == (2, -2)


# ---------------------------------------------------------------------------
# Web Mercator
# ---------------------------------------------------------------------------


def test_mercator_origin_is_the_origin() -> None:
    """Within a micrometre, not exactly.

    ``tan(pi/4)`` is 1 - 1.1e-16 in IEEE-754 rather than exactly 1, so the
    logarithm leaves a residue of about 7e-10 m. That is 0.7 nanometres, which
    is why the tolerance is a micron rather than a bit-exact zero: an exact
    assertion here would be testing the libm, not the projection.
    """
    x, y = lonlat_to_world(0.0, 0.0)
    assert x == 0.0
    assert y == pytest.approx(0.0, abs=1e-6)


def test_mercator_half_world_width() -> None:
    """The standard 20037508.34 m half-extent, reached at 90 degrees."""
    x, _ = lonlat_to_world(90.0, 0.0)
    assert x == pytest.approx(math.pi * WEB_MERCATOR_RADIUS_METERS / 2, rel=1e-9)
    # 180 folds to -180 by the half-open range, and the two name the same
    # meridian, so the far edge is negative.
    far_east, _ = lonlat_to_world(180.0, 0.0)
    assert far_east == pytest.approx(lonlat_to_world(-180.0, 0.0)[0])
    assert far_east == pytest.approx(-20037508.342789244, rel=1e-9)


def test_mercator_is_square_at_the_clamp_latitude() -> None:
    """The defining property of the 85.05112878 limit: x and y extents match,
    so the whole world fits one square."""
    _, y = lonlat_to_world(0.0, WORLD_MAX_LATITUDE_MERCATOR)
    assert y == pytest.approx(20037508.342789244, rel=1e-6)


def test_mercator_y_increases_northward() -> None:
    ys = [lonlat_to_world(0.0, lat)[1] for lat in range(-80, 81, 10)]
    assert ys == sorted(ys)


def test_pole_input_is_clamped_not_crashed() -> None:
    """A GPS fix at exactly 90 degrees must not produce an infinity. Without
    the clamp, tan(pi/4 + pi/2) is infinite and this is a 500."""
    x, y = lonlat_to_world(0.0, 90.0)
    assert math.isfinite(x) and math.isfinite(y)
    assert y == pytest.approx(20037508.342789244, rel=1e-6)
    assert clamp_latitude(90.0) == WORLD_MAX_LATITUDE_MERCATOR
    assert clamp_latitude(-90.0) == -WORLD_MAX_LATITUDE_MERCATOR
    assert clamp_latitude(12.0) == 12.0


def test_longitude_is_normalised_across_the_antimeridian() -> None:
    assert normalise_longitude(190.0) == pytest.approx(-170.0)
    assert normalise_longitude(-190.0) == pytest.approx(170.0)
    # The range is half-open, [-180, 180), so +180 folds to -180. Both name
    # the antimeridian; picking one and pinning it stops a sign flip from
    # moving a tile at the seam.
    assert normalise_longitude(180.0) == pytest.approx(-180.0)
    assert normalise_longitude(0.0) == pytest.approx(0.0)
    assert lonlat_to_world(190.0, 0.0) == pytest.approx(lonlat_to_world(-170.0, 0.0))


@pytest.mark.parametrize(
    "lon,lat",
    [(0, 0), (13.4, 52.5), (-74.0, 40.7), (151.2, -33.9), (0, 60.0), (-122.4, 37.8)],
)
def test_mercator_round_trip(lon: float, lat: float) -> None:
    x, y = lonlat_to_world(lon, lat)
    assert world_to_lonlat(x, y) == pytest.approx((lon, lat), abs=1e-6)


def test_ground_scale_is_cos_of_latitude() -> None:
    """The honest caveat on HEX_SIZE_METERS: plane metres are not ground
    metres, and the discrepancy grows with latitude."""
    assert ground_distance_scale(0.0) == pytest.approx(1.0)
    assert ground_distance_scale(60.0) == pytest.approx(0.5)
    assert ground_distance_scale(40.0) == pytest.approx(math.cos(math.radians(40)))
    # A S=40 hex is roughly 69 m across in plane space, so ~53 m of real ground
    # at 40 degrees north.
    plane_span = SQRT3 * HEX_SIZE_METERS
    assert plane_span * ground_distance_scale(40.0) == pytest.approx(53.07, rel=0.01)


# ---------------------------------------------------------------------------
# Bounding box
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rings", [0, 1, 4, 9])
def test_bounding_box_actually_contains_every_corner(rings: int) -> None:
    """The 'exact bbox' claim, verified rather than asserted. An approximate
    box would clip the outer ring and the client would request a viewport that
    cuts off tiles it was already sent."""
    tiles = list(tiles_in_radius(0, 0, rings))
    box = tiles_bounding_box(tiles)

    for q, r in tiles:
        cx, cy = axial_to_world(q, r)
        for corner_x, corner_y in hex_corners(cx, cy):
            assert box.min_world_x <= corner_x <= box.max_world_x
            assert box.min_world_y <= corner_y <= box.max_world_y


def test_bounding_box_is_tight() -> None:
    """Each face must be touched by at least one corner, otherwise the box is
    bigger than it needs to be."""
    box = tiles_bounding_box(list(tiles_in_radius(0, 0, 3)))
    xs = []
    ys = []
    for q, r in tiles_in_radius(0, 0, 3):
        cx, cy = axial_to_world(q, r)
        for corner_x, corner_y in hex_corners(cx, cy):
            xs.append(corner_x)
            ys.append(corner_y)
    assert box.min_world_x == pytest.approx(min(xs))
    assert box.max_world_x == pytest.approx(max(xs))
    assert box.min_world_y == pytest.approx(min(ys))
    assert box.max_world_y == pytest.approx(max(ys))


def test_bounding_box_degrees_contain_the_centres() -> None:
    tiles = list(tiles_in_radius(12, -30, 5))
    box = tiles_bounding_box(tiles)
    for q, r in tiles:
        lon, lat = axial_to_lonlat(q, r)
        assert box.min_lon <= lon <= box.max_lon
        assert box.min_lat <= lat <= box.max_lat


# ---------------------------------------------------------------------------
# Real-world behaviour
# ---------------------------------------------------------------------------


def test_a_short_walk_moves_through_neighbouring_tiles() -> None:
    """Walk 200 m due north in 5 m increments at a real city coordinate. Each
    step must land on the same tile or a neighbour of it -- a jump of two or
    more rings would mean the projection is tearing."""
    lat, lon = 52.52, 13.405  # Berlin
    previous = lonlat_to_axial(lat, lon)

    metres_per_degree_lat = 111_320.0
    for metres in range(5, 205, 5):
        current = lonlat_to_axial(lat + metres / metres_per_degree_lat, lon)
        assert hex_distance(previous, current) <= 1, (
            f"a {metres} m walk jumped {hex_distance(previous, current)} rings"
        )
        previous = current


def test_one_tile_is_about_a_one_minute_walk() -> None:
    """The gameplay promise in HEX_SIZE_METERS: crossing a tile should cost a
    plausible amount of walking."""
    lat = 0.0
    metres_per_degree_lat = 111_320.0
    start = lonlat_to_axial(lat, 0.0)
    moved = 0.0
    tile = start
    while tile == start:
        moved += 5.0
        tile = lonlat_to_axial(lat + moved / metres_per_degree_lat, 0.0)
    # One hex is ~69 m of plane space; walking to the next centre is under
    # twice that. Guards against a scale error of 10x or 100x.
    assert 20.0 < moved <= 2 * SQRT3 * HEX_SIZE_METERS


def test_the_plane_saturates_beyond_the_mercator_limit() -> None:
    """Documented limitation of the world model, made executable.

    Web Mercator is unbounded in longitude but *not* in latitude: past
    85.05112878 the projection diverges, so ``lonlat_to_world`` clamps. Every
    tile whose true latitude exceeds that limit is therefore reported at the
    clamped latitude, and the round trip stops being invertible -- the tile you
    get back is not the tile you asked about.

    This is unreachable in practice (85 north is Arctic sea ice, not a
    university campus) but it is a genuine edge of the world model, the clamp
    is silent, and it is pinned here rather than left to be discovered.

    Coordinates are chosen with ``q = -r / 2``, which makes ``x`` exactly zero
    so that changing ``r`` moves purely in latitude.
    """
    tile = (-400_000, 800_000)

    _, reported_lat = axial_to_lonlat(*tile)
    assert reported_lat > WORLD_MAX_LATITUDE_MERCATOR, "precondition"

    # The forward direction is lossy out here...
    assert lonlat_to_axial(reported_lat, 0.0) != tile

    # ...but the reachable world is not. Everything a player could stand on
    # round-trips exactly, which is the guarantee the game actually relies on.
    for reachable in SAMPLE_TILES:
        lon, lat = axial_to_lonlat(*reachable)
        assert abs(lat) < WORLD_MAX_LATITUDE_MERCATOR
        assert lonlat_to_axial(lat, lon) == reachable


def test_the_reachable_plane_is_unbounded_east_west() -> None:
    """The plane does extend past the antimeridian -- and then wraps.

    ``world_to_lonlat`` is the mathematical inverse of the projection and is
    deliberately not folded into [-180, 180): doing so would hide *which* wrap
    a tile came from, and the plane is wider than one Mercator world. The
    consequence is that a tile far past the seam reports a longitude outside the
    normal range -- 1000 rings east of the origin is really 622.37 degrees,
    the same meridian as -97.63 -- and that meridian now carries two distinct
    tiles, one from each lap of the plane.

    Unreachable in practice (it takes 20,000 km of walking past the
    antimeridian) but it is a real property of an unbounded plane, so it is
    documented and tested rather than left as a surprise. The practical
    consequence for a client is that ``bounds.min_lon``/``max_lon`` can exceed
    +/-180 in this case.
    """
    far_lon, _ = axial_to_lonlat(1_000_000, 0)
    assert far_lon > 180.0
    assert far_lon == pytest.approx(622.37, abs=0.01)

    # The alias: the same real-world meridian, reached on a later lap.
    assert ((far_lon + 180.0) % 360.0) - 180.0 == pytest.approx(-97.63, abs=0.01)

    # Every tile a player could plausibly stand on stays in range.
    for q, r in SAMPLE_TILES:
        lon, lat = axial_to_lonlat(q, r)
        assert -180.0 <= lon < 180.0
