"""``world_max_radius_from_origin``: the optional arena boundary.

Unset by default, because the MVP plane is a real-world map and a player in
another country is not cheating by standing there. Set, it closes the one real
anti-cheat hole phase 5 documents: a spoofed first GPS fix seeding a territory
on the far side of the planet.

Two things are easy to get wrong and are pinned here. The boundary must apply to
a player's *first* claim, not only their later ones -- an exempt first claim
makes the setting a lie. And it must apply to the *map* as well as to claims,
or "fenced arena" means a player can see every territory and every owner's name
while being unable to touch any of it.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _funded(client, email: str, mana: int = 200) -> dict[str, str]:
    """Sign up over HTTP with a mana balance. Returns auth headers."""
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models.user import User

    response = client.post(
        "/auth/signup",
        json={"email": email, "password": "correct-horse-battery-staple"},
    )
    assert response.status_code == 201, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    with get_session_factory()() as session:
        user = session.scalar(select(User).where(User.email == email))
        user.mana = mana
        user.last_mana_regen_at = _dt.now(_tz.utc)
        session.commit()
    return headers


def _claim(client, headers, q: int, r: int):
    return client.post("/territory/claim", json={"q": q, "r": r}, headers=headers)


# ---------------------------------------------------------------------------
# Unset: inert
# ---------------------------------------------------------------------------


def test_unset_leash_allows_a_first_claim_anywhere(client, settings_override) -> None:
    """The default must be genuinely unbounded, not "bounded by accident".

    A default that quietly refused distant claims would make the game unplayable
    for anyone not near the origin, and it would do so with a 422 that reads
    like an adjacency problem.
    """
    settings_override(WORLD_MAX_RADIUS_FROM_ORIGIN=None)
    headers = _funded(client, "far@example.com")

    response = _claim(client, headers, 500, 500)

    assert response.status_code == 200, response.text


def test_unset_leash_allows_viewing_anywhere(client, settings_override) -> None:
    settings_override(WORLD_MAX_RADIUS_FROM_ORIGIN=None)

    response = client.get("/world", params={"lat": 0.0, "lon": 0.0, "rings": 3})

    assert response.status_code == 200
    assert response.json()["tile_count"] == 37


# ---------------------------------------------------------------------------
# Claim path
# ---------------------------------------------------------------------------


def test_a_first_claim_beyond_the_leash_is_refused(
    client, settings_override
) -> None:
    """The whole point: a first claim is not exempt from the boundary.

    Phase 5 exempts the first claim from *adjacency* deliberately, and that is
    what the leash exists to cover. A setting that only bound later claims would
    close nothing, because the attacker seeds the far territory first and never
    has to make a second claim.
    """
    settings_override(WORLD_MAX_RADIUS_FROM_ORIGIN=5)
    headers = _funded(client, "sneaky@example.com")

    response = _claim(client, headers, 500, 500)

    assert response.status_code == 422


def test_a_claim_exactly_on_the_boundary_is_allowed(
    client, settings_override
) -> None:
    """Inclusive, and pinned from both sides.

    An off-by-one that excluded the last ring would shrink the arena by one ring
    for no stated reason; one that included the next would not fence anything
    reliably.
    """
    settings_override(WORLD_MAX_RADIUS_FROM_ORIGIN=5)

    inside = _claim(client, _funded(client, "on@example.com"), 5, 0)
    outside = _claim(client, _funded(client, "off@example.com"), 6, 0)

    assert inside.status_code == 200, inside.text
    assert outside.status_code == 422


def test_a_later_claim_beyond_the_leash_is_refused(
    client, settings_override
) -> None:
    """Adjacency cannot get a player past the leash on its own.

    Adjacency forces growth one ring at a time from a legal seed, so in practice
    it bounds reach too -- but only if the seed was legal. A player who seeded
    before the setting was tightened keeps growing, and the leash has to stop
    them.
    """
    settings_override(WORLD_MAX_RADIUS_FROM_ORIGIN=3)
    headers = _funded(client, "creeper@example.com", mana=200)
    assert _claim(client, headers, 0, 0).status_code == 200
    # Grow legally to the boundary, then try to step past it.
    for q, r in [(1, 0), (2, 0), (3, 0)]:
        assert _claim(client, headers, q, r).status_code == 200

    beyond = _claim(client, headers, 4, 0)

    assert beyond.status_code == 422


def test_a_refused_claim_creates_no_tile_and_spends_no_mana(
    client, settings_override
) -> None:
    from sqlalchemy import func, select

    from app.db.session import get_session_factory
    from app.models.hex_tile import HexTile
    from app.models.user import User

    settings_override(WORLD_MAX_RADIUS_FROM_ORIGIN=3)
    headers = _funded(client, "sneaky@example.com")
    assert _claim(client, headers, 0, 0).status_code == 200

    # Read the balance *after* the legal claim, so the comparison isolates the
    # refused one.
    with get_session_factory()() as session:
        mana_before = session.scalar(
            select(User.mana).where(User.email == "sneaky@example.com")
        )

    refused = _claim(client, headers, 500, 500)

    with get_session_factory()() as session:
        tiles = session.scalar(select(func.count()).select_from(HexTile))
        mana_after = session.scalar(
            select(User.mana).where(User.email == "sneaky@example.com")
        )

    assert refused.status_code == 422
    assert tiles == 1
    assert mana_after == mana_before


def test_reclaiming_an_owned_tile_inside_the_leash_still_works(
    client, settings_override
) -> None:
    """A reclaim costs nothing, so the leash must not refuse it.

    A player panning the map re-claims their own tiles constantly. Refusing
    those would break the client for no security benefit -- the tile is already
    theirs, and it is inside the arena because they were allowed to take it.
    """
    settings_override(WORLD_MAX_RADIUS_FROM_ORIGIN=3)
    headers = _funded(client, "reclaimer@example.com")
    assert _claim(client, headers, 0, 0).status_code == 200

    reclaim = _claim(client, headers, 0, 0)

    assert reclaim.status_code == 200
    assert reclaim.json()["tile"]["is_new"] is False
    assert reclaim.json()["mana_spent"] == 0


def test_the_leash_centre_is_configurable(client, settings_override) -> None:
    """Moving the arena moves the claim rule with it.

    A game launched for one city and later expanded would re-centre rather than
    redeploy a new origin, and a leash pinned to (0, 0) would make that
    impossible.
    """
    settings_override(
        WORLD_MAX_RADIUS_FROM_ORIGIN=2,
        WORLD_ORIGIN_LAT="40.7128",
        WORLD_ORIGIN_LON="-74.0060",
    )
    from app.services.geo import lonlat_to_axial

    origin = lonlat_to_axial(40.7128, -74.0060)
    headers = _funded(client, "nyc@example.com")

    near = _claim(client, headers, origin[0] + 2, origin[1])
    far = _claim(client, _funded(client, "elsewhere@example.com"), 0, 0)

    assert near.status_code == 200, near.text
    assert far.status_code == 422


def test_the_leash_is_read_per_request(client, settings_override) -> None:
    """Configuration, not a constant baked in at import.

    The service is a long-lived import; reading the value at module scope would
    mean turning the arena on needs a redeploy of every worker. This is the test
    that would catch a regression to an import-time read.
    """
    headers = _funded(client, "later@example.com")

    settings_override(WORLD_MAX_RADIUS_FROM_ORIGIN=2)
    before = _claim(client, headers, 500, 500)

    settings_override(WORLD_MAX_RADIUS_FROM_ORIGIN=1000)
    after = _claim(client, headers, 500, 500)

    assert before.status_code == 422
    assert after.status_code == 200, after.text


# ---------------------------------------------------------------------------
# View path
# ---------------------------------------------------------------------------


def test_a_viewport_centre_outside_the_leash_is_refused(
    client, settings_override
) -> None:
    settings_override(WORLD_MAX_RADIUS_FROM_ORIGIN=3)

    response = client.get("/world", params={"lat": 40.0, "lon": 0.0, "rings": 3})

    assert response.status_code == 422


def test_a_viewport_inside_the_leash_is_clamped_not_refused(
    client, settings_override
) -> None:
    """Inside the arena the caller gets as much of the map as fits.

    Refusing here would be unhelpful: the centre is legal, the player just asked
    for a wide viewport near the edge. Clamping keeps the call useful and still
    never returns a tile outside the fence.
    """
    settings_override(WORLD_MAX_RADIUS_FROM_ORIGIN=5)

    response = client.get("/world", params={"lat": 0.0, "lon": 0.0, "rings": 16})
    body = response.json()

    assert response.status_code == 200
    # The default 16-ring request is truncated to the leash.
    assert body["rings"] == 5
    assert body["tile_count"] == 91  # 3*5*6+1


def test_a_clamped_viewport_never_returns_a_tile_outside_the_leash(
    client, settings_override
) -> None:
    """The property behind the clamp, checked rather than assumed.

    The clamp is arithmetic on hex distance, so the risk is an off-by-one that
    still returns a plausible-looking response. Asserting the invariant over
    every returned tile is what makes the clamp trustworthy.
    """
    from app.services.geo import hex_distance, lonlat_to_axial

    settings_override(WORLD_MAX_RADIUS_FROM_ORIGIN=4)
    origin = lonlat_to_axial(0.0, 0.0)

    response = client.get("/world", params={"lat": 0.0, "lon": 0.0, "rings": 16})

    assert response.status_code == 200
    for tile in response.json()["tiles"]:
        assert hex_distance(origin, (tile["q"], tile["r"])) <= 4, tile


def test_viewing_the_leash_centre_itself_is_unaffected(
    client, settings_override
) -> None:
    settings_override(WORLD_MAX_RADIUS_FROM_ORIGIN=8)

    response = client.get("/world", params={"lat": 0.0, "lon": 0.0, "rings": 4})
    body = response.json()

    assert response.status_code == 200
    assert body["rings"] == 4
    assert body["tile_count"] == 61  # 3*4*5+1, the unclamped count
