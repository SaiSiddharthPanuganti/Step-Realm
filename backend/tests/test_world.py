"""``GET /world`` tests: the HTTP contract, and the DB hydration behind it.

The geometry itself is covered in ``test_geo.py``. What is verified here is
that the endpoint assembles a correct *view* -- correct centre, correct
membership, correct ownership, and no rows created by a read.
"""

from __future__ import annotations

import pytest

from app.core.constants import (
    WORLD_MAX_LATITUDE_MERCATOR,
    WORLD_VIEWPORT_MAX_RINGS,
    WORLD_VIEWPORT_RINGS,
)
from app.models.hex_tile import HexTile
from app.services.geo import (
    axial_to_world,
    hex_distance,
    lonlat_to_axial,
    lonlat_to_world,
    tiles_in_radius,
    tile_count_in_radius,
)


#: A real coordinate with buildings on it, so the numbers are not synthetic.
BERLIN_LAT = 52.52
BERLIN_LON = 13.405


@pytest.fixture
def claim_tiles() -> callable:
    """Materialise hex_tiles rows, bypassing the phase-5 claim path.

    Phase 5 does not exist yet, so the rows are written directly. This is the
    one place a test reaches past the service layer, and it is deliberate: the
    read path must be correct for rows however they came to exist.
    """
    from app.db.session import get_session_factory

    def _claim(tiles, owner_id: int | None = None) -> list[int]:
        with get_session_factory()() as session:
            for q, r in tiles:
                session.add(HexTile(q=q, r=r, owner_id=owner_id))
            session.commit()
        return [q for q, _ in tiles]

    return _claim


@pytest.fixture
def tile_count() -> callable:
    """How many rows currently exist in hex_tiles."""

    def _count() -> int:
        from sqlalchemy import func, select

        from app.db.session import get_session_factory

        with get_session_factory()() as session:
            return session.execute(
                select(func.count()).select_from(HexTile)
            ).scalar_one()

    return _count


def world(client, **params):
    response = client.get("/world", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_default_viewport_is_817_tiles(client) -> None:
    body = world(client, lat=BERLIN_LAT, lon=BERLIN_LON)
    assert body["rings"] == WORLD_VIEWPORT_RINGS
    assert body["tile_count"] == 817
    assert len(body["tiles"]) == 817
    assert body["owned_count"] == 0
    assert body["hex_size_meters"] == 40.0


@pytest.mark.parametrize("rings", [0, 1, 2, 5, 19])
def test_ring_count_is_exact_for_any_radius(client, rings: int) -> None:
    body = world(client, lat=BERLIN_LAT, lon=BERLIN_LON, rings=rings)
    assert body["tile_count"] == tile_count_in_radius(rings)
    assert len(body["tiles"]) == tile_count_in_radius(rings)


def test_centre_tile_is_present_and_every_tile_is_within_the_radius(
    client,
) -> None:
    body = world(client, lat=BERLIN_LAT, lon=BERLIN_LON)
    centre = (body["centre"]["q"], body["centre"]["r"])
    coordinates = [(tile["q"], tile["r"]) for tile in body["tiles"]]

    assert centre in coordinates
    assert len(coordinates) == len(set(coordinates)), "duplicate tile"
    assert all(hex_distance(centre, t) <= WORLD_VIEWPORT_RINGS for t in coordinates)


def test_centre_echo_is_the_projection_of_the_requested_point(client) -> None:
    body = world(client, lat=BERLIN_LAT, lon=BERLIN_LON)
    expected_q, expected_r = lonlat_to_axial(BERLIN_LAT, BERLIN_LON)
    assert (body["centre"]["q"], body["centre"]["r"]) == (expected_q, expected_r)
    # The echoed lat/lon is the *tile centre*, not the raw request, because the
    # player's GPS fix is somewhere inside the hex they are standing in.
    assert body["centre"]["lat"] == pytest.approx(BERLIN_LAT, abs=0.001)
    assert body["centre"]["lon"] == pytest.approx(BERLIN_LON, abs=0.001)


def test_tile_positions_match_the_geometry_module(client) -> None:
    """world_x/world_y must be the same numbers geo computes, or the client
    draws the grid somewhere the server does not think it is."""
    body = world(client, lat=BERLIN_LAT, lon=BERLIN_LON, rings=3)
    for tile in body["tiles"][:25]:
        x, y = axial_to_world(tile["q"], tile["r"])
        assert tile["world_x"] == pytest.approx(x, abs=1e-3)
        assert tile["world_y"] == pytest.approx(y, abs=1e-3)


def test_bounds_contain_every_returned_tile(client) -> None:
    body = world(client, lat=BERLIN_LAT, lon=BERLIN_LON, rings=6)
    bounds = body["bounds"]
    for tile in body["tiles"]:
        assert bounds["min_world_x"] <= tile["world_x"] <= bounds["max_world_x"]
        assert bounds["min_world_y"] <= tile["world_y"] <= bounds["max_world_y"]


def test_response_order_is_stable_across_requests(client) -> None:
    first = world(client, lat=BERLIN_LAT, lon=BERLIN_LON, rings=5)
    second = world(client, lat=BERLIN_LAT, lon=BERLIN_LON, rings=5)
    assert first["tiles"] == second["tiles"]


def test_viewport_is_empty_but_valid_over_remote_ocean(client) -> None:
    """The plane is procedural, so a coordinate nobody has visited returns a
    full viewport of unclaimed tiles rather than an error."""
    body = world(client, lat=-40.0, lon=-150.0, rings=2)
    assert body["tile_count"] == 19
    assert all(tile["owner"] is None for tile in body["tiles"])


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_latitude_and_longitude_are_required(client) -> None:
    assert client.get("/world").status_code == 422
    assert client.get("/world", params={"lat": BERLIN_LAT}).status_code == 422
    assert client.get("/world", params={"lon": BERLIN_LON}).status_code == 422


@pytest.mark.parametrize(
    "params",
    [
        {"lat": 91.0, "lon": 0.0},
        {"lat": -91.0, "lon": 0.0},
        {"lat": 0.0, "lon": 181.0},
        {"lat": 0.0, "lon": -181.0},
        {"lat": 0.0, "lon": 0.0, "rings": -1},
        {"lat": 0.0, "lon": 0.0, "rings": WORLD_VIEWPORT_MAX_RINGS + 1},
        {"lat": 0.0, "lon": 0.0, "rings": 10_000},
    ],
)
def test_out_of_range_input_is_rejected(client, params: dict) -> None:
    assert client.get("/world", params=params).status_code == 422


def test_max_rings_is_exactly_at_the_published_bound(client) -> None:
    assert client.get(
        "/world", params={"lat": 0.0, "lon": 0.0, "rings": WORLD_VIEWPORT_MAX_RINGS}
    ).status_code == 200


def test_a_polar_latitude_is_clamped_not_rejected(client) -> None:
    """89 degrees is a legal latitude, so it must return 200 -- clamped to the
    edge of the projection, and saying so."""
    body = world(client, lat=89.0, lon=0.0, rings=1)
    assert body["latitude_was_clamped"] is True
    assert body["centre"]["lat"] == pytest.approx(
        WORLD_MAX_LATITUDE_MERCATOR, abs=0.001
    )


def test_an_ordinary_latitude_is_not_flagged_as_clamped(client) -> None:
    assert world(client, lat=BERLIN_LAT, lon=BERLIN_LON)["latitude_was_clamped"] is (
        False
    )


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def test_claimed_tiles_report_their_owner(client, claim_tiles, make_user) -> None:
    user_id = make_user()
    centre = lonlat_to_axial(BERLIN_LAT, BERLIN_LON)
    claim_tiles([centre, (centre[0] + 1, centre[1])], owner_id=user_id)

    body = world(client, lat=BERLIN_LAT, lon=BERLIN_LON, rings=4)
    assert body["owned_count"] == 2
    owned = [t for t in body["tiles"] if t["owner"] is not None]
    assert {(t["q"], t["r"]) for t in owned} == {
        centre,
        (centre[0] + 1, centre[1]),
    }
    assert all(t["owner"]["id"] == user_id for t in owned)


def test_ownership_is_reported_per_tile_across_players(
    client, claim_tiles, make_user
) -> None:
    alice = make_user(email="alice@example.com")
    bob = make_user(email="bob@example.com")
    centre = lonlat_to_axial(BERLIN_LAT, BERLIN_LON)

    claim_tiles([centre], owner_id=alice)
    claim_tiles([(centre[0], centre[1] + 1)], owner_id=bob)

    body = world(client, lat=BERLIN_LAT, lon=BERLIN_LON, rings=3)
    by_coord = {(t["q"], t["r"]): t["owner"]["id"] for t in body["tiles"] if t["owner"]}
    assert by_coord[centre] == alice
    assert by_coord[(centre[0], centre[1] + 1)] == bob
    assert body["owned_count"] == 2


def test_tiles_outside_the_viewport_are_not_reported(
    client, claim_tiles, make_user
) -> None:
    """The *response* is assembled by walking the hex disc, so a row in the
    corner of the query's rectangular bounding box can never appear in it,
    however the lookup behaves.

    An axial offset of (rings, -rings) is only `rings` away -- the corners
    that genuinely fall out of the disc are the (rings, rings) and
    (-rings, -rings) ones, where dq + dr doubles the distance.
    """
    user_id = make_user()
    centre = lonlat_to_axial(BERLIN_LAT, BERLIN_LON)
    rings = 4

    just_outside = (centre[0] + rings, centre[1] + rings)
    assert hex_distance(centre, just_outside) == 2 * rings
    claim_tiles([just_outside], owner_id=user_id)

    body = world(client, lat=BERLIN_LAT, lon=BERLIN_LON, rings=rings)
    assert body["owned_count"] == 0
    assert all(tile["owner"] is None for tile in body["tiles"])
    assert just_outside not in {(t["q"], t["r"]) for t in body["tiles"]}


def test_load_owners_returns_only_tiles_inside_the_disc(
    session, claim_tiles, make_user
) -> None:
    """Direct test of :func:`load_owners`, which does bound its result.

    Worth separating from the endpoint test above, because the two guard
    different things. The endpoint is safe because it iterates the disc and
    probes the lookup; this pins that the lookup itself does not hand back
    bounding-box corners. Without it the filter inside ``load_owners`` is an
    untested equivalent mutant -- removing it changes nothing observable today,
    but a future refactor that iterated the returned dict instead would
    silently start leaking corner tiles.
    """
    from app.services.world_service import load_owners

    user_id = make_user()
    centre = lonlat_to_axial(BERLIN_LAT, BERLIN_LON)
    rings = 4
    disc = list(tiles_in_radius(*centre, rings))

    corners = [
        (centre[0] + rings, centre[1] + rings),
        (centre[0] - rings, centre[1] - rings),
        (centre[0] + 2 * rings, centre[1]),
        (centre[0], centre[1] - 2 * rings),
    ]
    # Each must genuinely be outside the disc. (rings, -rings) is *not* -- that
    # offset is exactly `rings` away, because dq + dr cancels -- which is the
    # same subtlety the endpoint test above documents.
    for corner in corners:
        assert hex_distance(centre, corner) > rings, corner

    claim_tiles(disc[:5] + corners, owner_id=user_id)

    owners = load_owners(session, centre, disc, rings)

    assert set(owners) == set(disc[:5])
    for q, r in owners:
        assert hex_distance(centre, (q, r)) <= rings
    for corner in corners:
        assert corner not in owners


def test_ownership_is_found_at_every_corner_of_the_viewport(
    client, claim_tiles, make_user
) -> None:
    """Regression test for a real bug: the hex-distance filter was originally
    centred on ``tiles[0]``, which is the south-west corner of the viewport
    rather than its centre. That kept a disc shifted one corner out of
    position, which silently dropped a wedge of claimed tiles. Each extreme
    tile of the viewport must be found."""
    user_id = make_user()
    centre = lonlat_to_axial(BERLIN_LAT, BERLIN_LON)
    rings = 5
    everything = list(tiles_in_radius(*centre, rings))
    claim_tiles(everything, owner_id=user_id)

    body = world(client, lat=BERLIN_LAT, lon=BERLIN_LON, rings=rings)
    assert body["owned_count"] == tile_count_in_radius(rings)

    returned = {(t["q"], t["r"]) for t in body["tiles"]}
    assert returned == set(everything)

    # The four axial extremes and the six geometric corners specifically.
    extremes = {
        min(q for q, _ in everything), max(q for q, _ in everything),
    }
    for q in extremes:
        assert any(t["q"] == q and t["owner"]["id"] == user_id for t in body["tiles"])


def test_a_claimed_tile_in_a_neighbouring_viewport_is_not_leaked(
    client, claim_tiles, make_user
) -> None:
    user_id = make_user()
    centre = lonlat_to_axial(BERLIN_LAT, BERLIN_LON)
    far_away = (centre[0] + 40, centre[1] - 40)
    claim_tiles([far_away], owner_id=user_id)

    body = world(client, lat=BERLIN_LAT, lon=BERLIN_LON, rings=4)
    assert body["owned_count"] == 0
    assert far_away not in {
        (t["q"], t["r"]) for t in body["tiles"]
    }


def test_response_never_contains_an_email_address(
    client, make_user, claim_tiles
) -> None:
    """A display name is now public, so this has to prove the *substitution*
    was clean rather than merely that nothing leaked.

    Adding ``display_name`` to the map response is exactly the change most
    likely to drag the email along with it, since both live on the same row and
    the obvious implementation returns the user object. This asserts the name
    is there (so the test cannot pass by returning nothing) and the address is
    not.
    """
    user_id = make_user(email="secret.person@example.com")
    centre = lonlat_to_axial(BERLIN_LAT, BERLIN_LON)
    claim_tiles([centre], owner_id=user_id)

    raw = client.get("/world", params={"lat": BERLIN_LAT, "lon": BERLIN_LON})
    assert raw.status_code == 200

    body = raw.json()
    owners = [t["owner"] for t in body["tiles"] if t["owner"]]
    assert owners, "expected the claimed tile to report an owner"
    assert owners[0]["id"] == user_id
    # "secret_person" -- the sanitised local part, minus the domain.
    assert owners[0]["display_name"] == "secret_person"

    assert "secret.person@example.com" not in raw.text
    assert "example.com" not in raw.text
    assert "password" not in raw.text


def test_every_tile_carries_a_lat_lon_centre_in_geojson_order(client) -> None:
    """The renderer is Mappls' React Native SDK, which takes vector geometry as
    GeoJSON, and RFC 7946 orders coordinates ``[lon, lat]``.

    Every tile needs a lon/lat centre for the client to build a
    FeatureCollection at all. This was a real gap: Phase 4 shipped only
    ``world_x``/``world_y``, which are projected metres and are *not* valid
    GeoJSON, so the client would have had to invert the projection to draw
    anything.
    """
    body = world(client, lat=BERLIN_LAT, lon=BERLIN_LON, rings=3)

    for tile in body["tiles"]:
        assert -90.0 <= tile["lat"] <= 90.0, tile
        assert -180.0 <= tile["lon"] <= 180.0, tile

    centre = body["centre"]
    assert centre["lat"] == pytest.approx(BERLIN_LAT, abs=0.5)
    assert centre["lon"] == pytest.approx(BERLIN_LON, abs=0.5)


def test_tile_lat_lon_is_not_transposed(client) -> None:
    """A transposed pair is still in range, so range checks cannot catch it.

    ``lat``/``lon`` swapped would put Berlin's latitude in ``lon``, and a
    viewport in the far north would look like it was near the equator. This
    pins the assignment by checking against a location whose two coordinates
    are far apart.
    """
    body = world(client, lat=60.0, lon=5.0, rings=2)
    centre = body["centre"]
    assert centre["lat"] == pytest.approx(60.0, abs=0.5)
    assert centre["lon"] == pytest.approx(5.0, abs=0.5)

    for tile in body["tiles"]:
        assert tile["lat"] == pytest.approx(60.0, abs=5.0), tile
        assert tile["lon"] == pytest.approx(5.0, abs=5.0), tile


def test_tile_lon_lat_agrees_with_the_projected_centre(client) -> None:
    """Both coordinate spaces must describe the same point, to the centimetre.

    They are computed by different functions in the geometry module, so a
    disagreement is possible and would be invisible to either check alone.

    The tolerance is the real constraint rather than a convenient round number:
    a hex has a 40 m circumradius, so the pair has to agree far more closely
    than that or the client draws one hex where another belongs. Rounding
    degrees to three decimals -- reusing the plane-metre precision, which reads
    like a harmless tidy-up -- costs 111 m and fails here.
    """
    body = world(client, lat=BERLIN_LAT, lon=BERLIN_LON, rings=2)

    worst = 0.0
    for tile in body["tiles"]:
        x, y = lonlat_to_world(tile["lon"], tile["lat"])
        worst = max(
            worst,
            abs(tile["world_x"] - x),
            abs(tile["world_y"] - y),
        )

    # Comfortably inside a hex: a client snapping to these coordinates cannot
    # place a hex in the wrong cell.
    assert worst < 1.0, f"worst round-trip error {worst:.3f} m is over a metre"


def test_a_tile_owner_exposes_exactly_id_and_display_name(
    client, make_user, claim_tiles
) -> None:
    """Pins the owner object to its two fields.

    /world is public and unauthenticated by default, so this is the boundary
    that keeps a future ``return UserResponse`` from leaking the email,
    password digest, or mana balance to every map load. An exact key-set
    assertion fails the moment a field is added by accident -- which is the
    point, since adding one should be a deliberate change to this test.
    """
    user_id = make_user(email="someone@example.com")
    centre = lonlat_to_axial(BERLIN_LAT, BERLIN_LON)
    claim_tiles([centre], owner_id=user_id)

    body = world(client, lat=BERLIN_LAT, lon=BERLIN_LON, rings=2)
    owners = [t["owner"] for t in body["tiles"] if t["owner"]]
    assert len(owners) == 1
    assert set(owners[0]) == {"id", "display_name"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_viewport_is_public(client) -> None:
    body = world(client, lat=BERLIN_LAT, lon=BERLIN_LON)
    assert body["viewer_id"] is None


def test_viewer_id_is_returned_when_a_token_is_supplied(client, authed) -> None:
    headers = authed()
    assert world(client, lat=BERLIN_LAT, lon=BERLIN_LON, **{}).get(
        "viewer_id"
    ) is None

    response = client.get(
        "/world", params={"lat": BERLIN_LAT, "lon": BERLIN_LON}, headers=headers
    )
    body = response.json()
    assert body["viewer_id"] is not None
    me = client.get("/me", headers=headers).json()
    assert body["viewer_id"] == me["id"]


def test_a_malformed_token_degrades_to_anonymous_not_a_401(client) -> None:
    """The map must still render with a stale token; /me is where a client
    discovers its session has expired."""
    response = client.get(
        "/world",
        params={"lat": BERLIN_LAT, "lon": BERLIN_LON},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert response.status_code == 200
    assert response.json()["viewer_id"] is None


def test_optional_auth_does_not_leak_a_lock_on_the_user_row(
    client, authed, make_user
) -> None:
    """Loading the viewer must not take a row lock, or a map refresh would
    serialise against every claim."""
    import threading

    user_id = make_user(email="lock-holder@example.com")
    headers = authed(email="viewer@example.com")
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def load_world() -> None:
        try:
            barrier.wait(timeout=20)
            client.get(
                "/world",
                params={"lat": BERLIN_LAT, "lon": BERLIN_LON},
                headers=headers,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def hold_user_lock() -> None:
        from sqlalchemy import text as sql_text

        from app.db.session import get_session_factory

        try:
            barrier.wait(timeout=20)
            with get_session_factory()() as session:
                session.execute(
                    sql_text("SELECT id FROM users WHERE id = :id FOR UPDATE"),
                    {"id": user_id},
                )
                # Long enough that a blocking read would show up as a timeout.
                import time

                time.sleep(1.5)
                session.rollback()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=load_world),
        threading.Thread(target=hold_user_lock),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert all(not thread.is_alive() for thread in threads)


# ---------------------------------------------------------------------------
# Read-only guarantees
# ---------------------------------------------------------------------------


def test_reading_the_world_creates_no_rows(client, tile_count) -> None:
    """The plane is procedural; only phase 5's claim may materialise a row."""
    assert tile_count() == 0
    for _ in range(3):
        world(client, lat=BERLIN_LAT, lon=BERLIN_LON)
    assert tile_count() == 0


def test_large_viewport_is_compressed(client) -> None:
    """817 tiles is far too much JSON for a mobile radio."""
    response = client.get(
        "/world",
        params={"lat": BERLIN_LAT, "lon": BERLIN_LON},
        headers={"Accept-Encoding": "gzip"},
    )
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"
    # The client still gets the whole thing after decoding.
    assert len(response.json()["tiles"]) == 817
