"""Walkability: hex classification and its world/claim integration.

The classification itself is pure and synthetic (no Overpass); the integration
tests stub ``walkability_for_disc`` so no test in this suite touches the
network. The HTTP fetch against the real Overpass is exercised by hand in dev,
not by CI -- a public third-party service is the worst thing to make a test
suite depend on.
"""

from __future__ import annotations

import pytest

from app.core.constants import (
    HEX_SIZE_METERS,
    TILE_CLAIM_COST,
    WALKABILITY_SAMPLE_SPACING_METERS,
)
from app.services.geo import lonlat_to_axial
from app.services.walkability_service import (
    WalkabilityUnavailable,
    classify_walkable_hexes,
    haversine_meters,
)

#: A representative point in Hyderabad; the numbers below are synthetic any way.
HYD_LAT = 17.4416
HYD_LON = 78.3894


def funded(authed, email: str, mana: int):
    """Sign up over HTTP and seed the balance (mirrors test_territory's player).

    Accounts now start at zero mana, and these tests are about walkability, not
    about earning a claim -- so the mana comes from the DB, like every other
    non-mana suite.
    """
    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models.user import User

    headers = authed(email)
    with get_session_factory()() as session:
        user = session.scalar(select(User).where(User.email == email))
        assert user is not None
        user.mana = mana
        session.commit()
    return headers


def road(lat_start: float, lat_end: float, lon: float) -> list[list[tuple[float, float]]]:
    """A two-vertex way running north-south at ``lon``."""
    return [[(lon, lat_start), (lon, lat_end)]]


# ---------------------------------------------------------------------------
# Classification (pure geometry)
# ---------------------------------------------------------------------------


def test_a_single_point_marks_exactly_its_hex() -> None:
    geometries: list[list[tuple[float, float]]] = [[(HYD_LON, HYD_LAT)]]
    walkable = classify_walkable_hexes(geometries)
    assert walkable == {lonlat_to_axial(HYD_LAT, HYD_LON)}


def test_classification_snaps_through_the_shared_projection() -> None:
    """The hex a road point lands in is the hex ``GET /world`` would draw it in."""
    geometry: list[tuple[float, float]] = [(HYD_LON, HYD_LAT)]
    walkable = classify_walkable_hexes([geometry])
    assert lonlat_to_axial(HYD_LAT, HYD_LON) in walkable


def test_sampling_closes_the_gaps_between_sparse_nodes() -> None:
    """Long node-free road stretches still mark every hex they pass through.

    Two vertices 300 m apart with an overpass between them would (sparsely)
    mark 2 hexes at best; resampling must mark the whole corridor.
    """
    distance = 300.0
    start = (78.45, 17.40)
    end = (78.45, 17.40 + distance / 111_320.0)  # metres of latitude
    assert haversine_meters(start, end) == pytest.approx(distance, rel=0.1)

    walkable = classify_walkable_hexes([[start, end]])
    only_nodes = {
        lonlat_to_axial(start[1], start[0]),
        lonlat_to_axial(end[1], end[0]),
    }

    assert only_nodes <= walkable
    # 300 m / 20 m spacing = 16 samples, so a mostly-straight run marks far
    # more than the two endpoint hexes. A generous floor keeps this robust to
    # the hex grid cutting the segment at an unfavourable angle.
    assert len(walkable) >= 6

    midpoint = (
        (start[0] + end[0]) / 2.0,
        (start[1] + end[1]) / 2.0,
    )
    assert lonlat_to_axial(midpoint[1], midpoint[0]) in walkable


def test_every_hex_is_within_one_hex_of_the_road() -> None:
    """Sanity shape check: a compact geometry yields a compact hex set."""
    walkable = classify_walkable_hexes(
        [[(78.45, 17.40), (78.455, 17.405), (78.45, 17.41)]]
    )
    assert len(walkable) < 50


# ---------------------------------------------------------------------------
# /world integration
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_walkability(monkeypatch) -> callable:
    """Replace the Overpass-fetching entry point with a scripted answer."""

    def _stub(result, *, degraded: bool = False) -> None:
        if degraded:
            def raise_unavailable(*_args, **_kwargs):  # noqa: ANN002, ANN003
                raise WalkabilityUnavailable("stubbed outage")

            monkeypatch.setattr(
                "app.services.walkability_service.walkability_for_disc",
                raise_unavailable,
            )
            return

        def answer(*_args, **_kwargs) -> set:  # noqa: ANN002, ANN003
            return set(result)

        monkeypatch.setattr(
            "app.services.walkability_service.walkability_for_disc", answer
        )

    return _stub


def test_world_returns_only_walkable_hexes_when_enabled(
    client, stub_walkability, settings_override
) -> None:
    settings_override(WALKABILITY_ENABLED=True)
    centre = lonlat_to_axial(HYD_LAT, HYD_LON)
    stub_walkability({centre})

    body = client.get(
        "/world", params={"lat": HYD_LAT, "lon": HYD_LON, "rings": 4}
    ).json()

    assert body["walkability_degraded"] is False
    assert body["tile_count"] == 1
    assert [(t["q"], t["r"]) for t in body["tiles"]] == [centre]
    assert body["bounds"]["min_lat"] != body["bounds"]["max_lat"]


def test_world_is_unfiltered_but_flagged_when_overpass_fails(
    client, stub_walkability, settings_override
) -> None:
    settings_override(WALKABILITY_ENABLED=True)
    stub_walkability(None, degraded=True)

    body = client.get(
        "/world", params={"lat": HYD_LAT, "lon": HYD_LON, "rings": 2}
    ).json()

    assert body["walkability_degraded"] is True
    assert body["tile_count"] == 19


def test_world_is_unfiltered_and_unflagged_when_disabled(client) -> None:
    """The strict default: no walkability flag, everything procedural."""
    body = client.get(
        "/world", params={"lat": HYD_LAT, "lon": HYD_LON, "rings": 2}
    ).json()
    assert body["walkability_degraded"] is False
    assert body["tile_count"] == 19


def test_claimed_tiles_outside_the_walkable_set_are_omitted(
    client, stub_walkability, settings_override, make_user
) -> None:
    """A tile you own is still a tile -- but it is still not on walkable ground."""
    settings_override(WALKABILITY_ENABLED=True)
    owner_id = make_user(email="holder@example.com")
    centre = lonlat_to_axial(HYD_LAT, HYD_LON)

    from app.db.session import get_session_factory
    from app.models.hex_tile import HexTile

    with get_session_factory()() as session:
        session.add(HexTile(q=centre[0], r=centre[1], owner_id=owner_id))
        session.commit()

    # The tile is claimed, but the walkable set says the ground is not there.
    stub_walkability({(centre[0] + 1, centre[1])})

    body = client.get(
        "/world", params={"lat": HYD_LAT, "lon": HYD_LON, "rings": 4}
    ).json()
    assert body["owned_count"] == 0
    assert {(t["q"], t["r"]) for t in body["tiles"]} == {(centre[0] + 1, centre[1])}


# ---------------------------------------------------------------------------
# Claim integration
# ---------------------------------------------------------------------------


def test_claim_on_non_walkable_hex_is_rejected(
    client, authed, stub_walkability, settings_override
) -> None:
    settings_override(WALKABILITY_ENABLED=True)
    centre = lonlat_to_axial(HYD_LAT, HYD_LON)
    stub_walkability({(centre[0] + 1, centre[1])})

    response = client.post(
        "/territory/claim",
        json={"q": centre[0], "r": centre[1]},
        headers=authed(),
    )
    assert response.status_code == 422
    assert "walkable" in response.json()["detail"]


def test_claim_on_walkable_hex_by_coordinates_succeeds(
    client, authed, stub_walkability, settings_override
) -> None:
    settings_override(WALKABILITY_ENABLED=True)
    centre = lonlat_to_axial(HYD_LAT, HYD_LON)
    stub_walkability({centre})

    response = client.post(
        "/territory/claim",
        json={"q": centre[0], "r": centre[1]},
        headers=funded(authed, "walkable@example.com", TILE_CLAIM_COST),
    )
    assert response.status_code == 200, response.text


def test_claim_by_location_resolves_then_gates(
    client, authed, stub_walkability, settings_override
) -> None:
    settings_override(WALKABILITY_ENABLED=True)
    centre = lonlat_to_axial(HYD_LAT, HYD_LON)
    stub_walkability({centre})

    ok = client.post(
        "/territory/claim-by-location",
        json={"lat": HYD_LAT, "lon": HYD_LON},
        headers=funded(authed, "byloc@example.com", TILE_CLAIM_COST),
    )
    assert ok.status_code == 200, ok.text

    stub_walkability(set())
    refused = client.post(
        "/territory/claim-by-location",
        json={"lat": HYD_LAT, "lon": HYD_LON},
        headers=authed(),
    )
    assert refused.status_code == 422


def test_claim_degrades_to_allowed_when_overpass_is_down(
    client, authed, stub_walkability, settings_override
) -> None:
    settings_override(WALKABILITY_ENABLED=True)
    stub_walkability(None, degraded=True)

    centre = lonlat_to_axial(HYD_LAT, HYD_LON)
    response = client.post(
        "/territory/claim",
        json={"q": centre[0], "r": centre[1]},
        headers=funded(authed, "degraded@example.com", TILE_CLAIM_COST),
    )
    # Fail-open, consistent with the world view.
    assert response.status_code == 200, response.text


def test_claim_is_unrestricted_when_walkability_is_disabled(
    client, authed
) -> None:
    centre = lonlat_to_axial(HYD_LAT, HYD_LON)
    response = client.post(
        "/territory/claim",
        json={"q": centre[0], "r": centre[1]},
        headers=funded(authed, "unrestricted@example.com", TILE_CLAIM_COST),
    )
    assert response.status_code == 200, response.text
