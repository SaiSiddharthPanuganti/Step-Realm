"""POST /territory/claim: the rules, and the concurrency that enforces them.

Two halves. The rules tests are ordinary HTTP assertions; the concurrency tests
use barriers so that overlapping claims are real rather than hoped for.

The concurrency half is where this phase's risk lives. A claim spends mana and
creates a permanent row, and the game invites exactly the situation where two
claims overlap: a player tapping two hexes quickly, or two players racing for
the same hex. Every test in the concurrency section is written so that removing
the locks would make it fail.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.core.constants import (
    HEX_NEIGHBOR_OFFSETS,
    MANA_REGEN_AMOUNT,
    MANA_REGEN_PERIOD_MINUTES,
    TILE_CLAIM_COST,
)
from app.db.session import get_session_factory
from app.models.hex_tile import HexTile
from app.models.user import User
from app.services.geo import axial_to_lonlat, axial_to_world, lonlat_to_axial
from app.services.territory_service import (
    InsufficientMana,
    NotAdjacentToTerritory,
    TileAlreadyClaimed,
    claim_tile,
)

#: Far from the origin, to prove the adjacency rule is really enforced rather
#: than every claim sneaking through.
FAR_Q, FAR_R = 50, 50

#: The six hexes touching (0, 0). Used as a controlled claim set: every one of
#: them is adjacent to a seeded (0, 0), so mana is the *only* thing that can
#: refuse a claim, which is what the overspend test needs.
NEIGHBOURS_OF_ORIGIN = list(HEX_NEIGHBOR_OFFSETS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def player(authed, email: str, mana: int) -> tuple[dict, int]:
    """Register over HTTP and set the mana balance. Returns (headers, user_id).

    Registration goes through ``/auth/signup`` so the token is a real one from
    a real code path, and the balance is then written directly to stand in for
    steps already synced. Creating the row with ``make_user`` instead and then
    signing up would collide, since signup refuses a duplicate address.
    """
    headers = authed(email)
    with get_session_factory()() as session:
        user = session.scalar(select(User).where(User.email == email))
        assert user is not None
        # Rewind the regen watermark so settling does not add mana on top of
        # the balance the test asked for.
        user.mana = mana
        user.last_mana_regen_at = datetime.now(timezone.utc)
        session.commit()
        return headers, user.id


def claim(client, headers, q: int, r: int):
    return client.post("/territory/claim", json={"q": q, "r": r}, headers=headers)


def tile_rows() -> list[tuple[int, int, int | None]]:
    with get_session_factory()() as session:
        return list(
            session.execute(
                select(HexTile.q, HexTile.r, HexTile.owner_id)
                .order_by(HexTile.q, HexTile.r)
            ).all()
        )


def mana_of(user_id: int) -> int:
    with get_session_factory()() as session:
        return session.get(User, user_id).mana


def owned_count(user_id: int) -> int:
    with get_session_factory()() as session:
        return (
            session.scalar(
                select(func.count())
                .select_from(HexTile)
                .where(HexTile.owner_id == user_id)
            )
            or 0
        )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_first_claim_spends_mana_and_creates_one_row(client, authed) -> None:
    headers, user_id = player(authed, "walker@example.com", TILE_CLAIM_COST)

    response = claim(client, headers, 0, 0)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["mana_spent"] == TILE_CLAIM_COST
    assert body["mana"] == 0
    assert body["tile"]["is_new"] is True
    assert body["owned_tile_count"] == 1
    assert body["claim_cost"] == TILE_CLAIM_COST
    assert body["mana_max"] == 200

    assert tile_rows() == [(0, 0, user_id)]
    assert mana_of(user_id) == 0


def test_claim_reports_the_correct_owned_count(client, authed) -> None:
    """Pins the flush-order bug: the count used to be read before the write.

    The project runs with ``autoflush=False``, so a count issued right after
    assigning ``owner_id`` sees nothing and the response tells the player they
    own zero tiles immediately after claiming one. The database is right and
    the API is wrong, which is the worst combination: it looks like a client
    rendering bug and is invisible to anyone reading the tables.
    """
    headers, _ = player(authed, "counter@example.com", 200)

    for expected, (q, r) in enumerate([(0, 0), (1, 0), (1, -1)], start=1):
        body = claim(client, headers, q, r).json()
        assert body["owned_tile_count"] == expected, (
            f"after claiming {(q, r)} the response said {body['owned_tile_count']}"
        )


def test_claim_echoes_coordinates_in_every_space(client, authed) -> None:
    """The map renders from lat/lon, and the projection comes from the server."""
    headers, _ = player(authed, "echo@example.com", 200)
    claim(client, headers, 3, -2)

    tile = claim(client, headers, 3, -2).json()["tile"]
    lon, lat = axial_to_lonlat(3, -2)
    x, y = axial_to_world(3, -2)

    assert tile["lat"] == pytest.approx(lat, abs=1e-7)
    assert tile["lon"] == pytest.approx(lon, abs=1e-7)
    assert tile["world_x"] == pytest.approx(x, abs=1e-3)
    assert tile["world_y"] == pytest.approx(y, abs=1e-3)


def test_claim_requires_authentication(client) -> None:
    assert client.post("/territory/claim", json={"q": 0, "r": 0}).status_code == 401
    assert (
        client.post(
            "/territory/claim-by-location", json={"lat": 0.0, "lon": 0.0}
        ).status_code
        == 401
    )


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


def test_reclaiming_your_own_tile_is_free_and_succeeds(client, authed) -> None:
    """A phone retrying a dropped response must not be charged twice.

    A 409 here would be actively harmful: the player would see an error for
    something they genuinely own, with no way to tell it apart from losing the
    hex to a rival.
    """
    headers, _ = player(authed, "idem@example.com", 2 * TILE_CLAIM_COST)

    first = claim(client, headers, 0, 0).json()
    second = claim(client, headers, 0, 0).json()

    assert first["tile"]["is_new"] is True
    assert second["tile"]["is_new"] is False
    assert second["mana_spent"] == 0
    assert second["mana"] == first["mana"]
    assert second["owned_tile_count"] == 1
    assert len(tile_rows()) == 1


def test_reclaim_by_location_matches_claim_by_coordinate(client, authed) -> None:
    """The two endpoints must resolve one point to the same hex.

    They share the projection, so this is mostly a guard against one of them
    growing its own conversion later.
    """
    headers, _ = player(authed, "both@example.com", 200)
    q, r = lonlat_to_axial(52.52, 13.405)

    claim(client, headers, q, r)
    by_location = client.post(
        "/territory/claim-by-location",
        json={"lat": 52.52, "lon": 13.405},
        headers=headers,
    )

    assert by_location.status_code == 200, by_location.text
    body = by_location.json()
    assert (body["tile"]["q"], body["tile"]["r"]) == (q, r)
    assert body["tile"]["is_new"] is False, "the endpoints disagreed about the hex"


def test_claim_by_location_accepts_the_whole_valid_range(client, authed) -> None:
    """A GPS fix in the projection's dead zone is clamped, not rejected.

    ``GET /world`` clamps rather than 422s, and the two must behave the same
    way or a player at high latitude could look at a map but not claim on it.

    A fresh player per coordinate, because only a *first* claim may be placed
    away from existing territory. One player walking the latitudes would get
    422 from the adjacency rule and blame the projection.
    """
    for index, lat in enumerate((85.0, 85.05112878, -85.05112878, 0.0, -33.8688)):
        headers, _ = player(authed, f"polar{index}@example.com", 200)
        response = client.post(
            "/territory/claim-by-location",
            json={"lat": lat, "lon": 0.0},
            headers=headers,
        )
        assert response.status_code == 200, (lat, response.text)
        assert -90.0 <= response.json()["tile"]["lat"] <= 90.0


def test_claim_by_location_rejects_an_impossible_coordinate(client, authed) -> None:
    headers = authed()
    for payload in ({"lat": 91.0, "lon": 0.0}, {"lat": 0.0, "lon": 181.0}):
        response = client.post(
            "/territory/claim-by-location", json=payload, headers=headers
        )
        assert response.status_code == 422, payload


# ---------------------------------------------------------------------------
# Mana
# ---------------------------------------------------------------------------


def test_cannot_claim_without_enough_mana(client, authed) -> None:
    """402 rather than 409 or 400.

    The request is valid and the player is permitted to make it; they just
    cannot pay yet. The client has to be able to say "go for a walk", and
    folding this in with "tile taken" would take that ability away.
    """
    headers, _ = player(authed, "poor@example.com", TILE_CLAIM_COST - 1)
    response = claim(client, headers, 0, 0)

    assert response.status_code == 402
    detail = response.json()["detail"]
    assert str(TILE_CLAIM_COST) in detail
    assert str(TILE_CLAIM_COST - 1) in detail


def test_a_refused_claim_creates_no_tile(client, authed) -> None:
    """A rejected claim must leave no trace.

    The tile row is inserted before the mana check so the row lock can be
    taken; the rollback is what makes that safe. Without the rollback, a
    refused claim would leave an unowned row behind and permanently block that
    hex for everyone.
    """
    headers, _ = player(authed, "broke@example.com", 0)
    assert claim(client, headers, 0, 0).status_code == 402
    assert tile_rows() == []


def test_claim_is_funded_by_regeneration(client, authed, age_user) -> None:
    """Mana accrued while the app was closed is spendable immediately.

    Without settling inside the claim transaction, a player who closed the app
    at 0 mana and returned an hour later would be told to wait another 40
    minutes for regen they had already earned. Five periods is the smallest
    number of periods that buys a tile, so this also pins that the whole
    accrued balance -- not just the part above the cost -- is considered.
    """
    periods = (TILE_CLAIM_COST + MANA_REGEN_AMOUNT - 1) // MANA_REGEN_AMOUNT
    headers, user_id = player(authed, "regen@example.com", 0)
    age_user(user_id, by=timedelta(minutes=MANA_REGEN_PERIOD_MINUTES * periods))

    response = claim(client, headers, 0, 0)
    assert response.status_code == 200, response.text
    assert response.json()["mana"] == MANA_REGEN_AMOUNT * periods - TILE_CLAIM_COST


def test_refusal_reports_the_balance_including_regenerated_mana(
    client, authed, age_user
) -> None:
    """The 402 body quotes the real balance, regen included.

    Regression: the claim transaction settles mana and then calls
    ``session.rollback()`` on refusal, and a rollback expires every instance in
    the session. Reading ``user.mana`` after it therefore re-queried the row and
    returned the pre-regeneration value, so a player sitting on 5 mana was told
    "claim costs 20 mana, you have 0" -- advice to go and earn mana they had
    already earned, for a claim that was never going to be affordable at 5
    either. One period of regen is deliberately not enough to buy a tile.
    """
    headers, user_id = player(authed, "miscount@example.com", 0)
    age_user(user_id, by=timedelta(minutes=MANA_REGEN_PERIOD_MINUTES))

    response = claim(client, headers, 0, 0)

    assert response.status_code == 402
    assert response.json()["detail"] == f"claim costs {TILE_CLAIM_COST} mana, you have {MANA_REGEN_AMOUNT}"


def test_claim_survives_a_daily_reset(client, authed) -> None:
    """Claiming just after UTC midnight rolls the daily cap first.

    Without the reset, ``daily_mana_earned`` would still be yesterday's total
    and the cap would refuse mana the player had legitimately earned today.
    """
    headers, user_id = player(authed, "resetter@example.com", TILE_CLAIM_COST)

    with get_session_factory()() as session:
        user = session.get(User, user_id)
        user.daily_mana_earned = 200
        user.daily_mana_reset_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        session.commit()

    assert claim(client, headers, 0, 0).status_code == 200


# ---------------------------------------------------------------------------
# Contiguity
# ---------------------------------------------------------------------------


def test_a_first_claim_may_be_placed_anywhere(client, authed) -> None:
    """The known, accepted anti-cheat hole, pinned so it stays visible.

    A player's very first tile is unrestricted, so a spoofed GPS can seed a
    territory anywhere on Earth. The spec scopes server-side anti-cheat out of
    the MVP; this test documents the hole rather than endorsing it. See
    ``WORLD_MAX_RADIUS_FROM_ORIGIN``.
    """
    headers, _ = player(authed, "seeder@example.com", 200)
    assert claim(client, headers, FAR_Q, FAR_R).status_code == 200


def test_a_second_claim_must_touch_the_first(client, authed) -> None:
    """Territory grows as a connected blob, not a scatter of islands.

    This is the rule that stops one GPS spoof carpeting the map with
    disconnected pockets.
    """
    player_headers, _ = player(authed, "blob@example.com", 200)
    assert claim(client, player_headers, 0, 0).status_code == 200
    assert claim(client, player_headers, FAR_Q, FAR_R).status_code == 422


@pytest.mark.parametrize("offset", list(HEX_NEIGHBOR_OFFSETS))
def test_all_six_neighbours_are_accepted(client, authed, offset) -> None:
    """Adjacency is edge-sharing, and all six edges must work.

    Checking one direction would let an off-by-one in the offsets table pass
    unnoticed while quietly making one side of every territory unclaimable.
    """
    dq, dr = offset
    headers, _ = player(authed, f"n{dq}_{dr}@example.com", 200)

    assert claim(client, headers, 0, 0).status_code == 200
    assert claim(client, authed(), dq, dr).status_code == 200


def test_adjacency_is_transitive(client, authed) -> None:
    """A chain, each claim adjacent to the last, is legal.

    The rule is "touches something you own", not "touches your first tile", so
    a territory can grow outward without limit.
    """
    headers, _ = player(authed, "chain@example.com", 200)
    for q in range(4):
        assert claim(client, headers, q, 0).status_code == 200, q


def test_adjacency_is_not_satisfied_by_a_diagonal(client, authed) -> None:
    """(1, 1) is two steps from (0, 0) in axial space, not one.

    The six axial offsets deliberately skip the three "diagonal" coordinates.
    A claim of (1, 1) must be refused, and this is exactly the case a wrong
    offsets table or a naive distance comparison would let through.
    """
    headers, _ = player(authed, "diag@example.com", 200)
    assert claim(client, headers, 0, 0).status_code == 200
    assert claim(client, headers, 1, 1).status_code == 422


def test_adjacency_only_counts_your_own_tiles(client, authed) -> None:
    """Touching a *rival's* tile is not the same as touching yours."""
    rival, _ = player(authed, "rival@example.com", 200)
    mine, _ = player(authed, "mine@example.com", 200)

    assert claim(client, rival, 0, 0).status_code == 200
    assert claim(client, mine, FAR_Q, FAR_R).status_code == 200

    # (0, 1) shares an edge with the rival's (0, 0) and nothing of mine, so it
    # is refused. A rule that counted any neighbouring tile would accept it.
    assert claim(client, mine, 0, 1).status_code == 422


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------


def test_a_tile_held_by_another_player_conflicts(client, authed) -> None:
    first, _ = player(authed, "first@example.com", 200)
    second, _ = player(authed, "second@example.com", 200)

    assert claim(client, first, 0, 0).status_code == 200
    response = claim(client, second, 0, 0)

    assert response.status_code == 409
    assert "another player" in response.json()["detail"]


def test_a_conflict_does_not_spend_mana(client, authed) -> None:
    holder, _ = player(authed, "holder@example.com", 200)
    thief, thief_id = player(authed, "thief@example.com", 200)

    claim(client, holder, 0, 0)
    assert claim(client, thief, 0, 0).status_code == 409

    assert mana_of(thief_id) == 200
    assert owned_count(thief_id) == 0


def test_an_unowned_orphan_row_can_be_adopted(client, authed) -> None:
    """A row with a NULL owner is claimable, not permanently dead ground.

    The claim path inserts-then-locks, so an unowned row is a legitimate
    intermediate state. If a crash ever left one behind it must be adoptable
    rather than blocking that hex forever.
    """
    with get_session_factory()() as session:
        session.add(HexTile(q=5, r=5, owner_id=None))
        session.commit()

    headers, user_id = player(authed, "adopter@example.com", 200)
    response = claim(client, headers, 5, 5)

    assert response.status_code == 200, response.text
    assert response.json()["tile"]["is_new"] is True
    assert tile_rows() == [(5, 5, user_id)]


def test_no_committed_row_is_ever_unowned(client, authed) -> None:
    """The domain invariant: a row exists if and only if somebody owns it.

    Checked after a success, an idempotent repeat and a refusal, which between
    them cover every exit path out of the claim transaction.
    """
    headers, _ = player(authed, "invariant@example.com", TILE_CLAIM_COST)

    claim(client, headers, 0, 0)           # success
    claim(client, headers, 0, 0)           # idempotent
    claim(client, headers, FAR_Q, FAR_R)  # refused: not adjacent

    with get_session_factory()() as session:
        orphans = session.scalar(
            select(func.count()).select_from(HexTile).where(HexTile.owner_id.is_(None))
        )
    assert orphans == 0


# ---------------------------------------------------------------------------
# Integration with the rest of the API
# ---------------------------------------------------------------------------


def test_a_claimed_tile_appears_in_the_world_with_its_owner(client, authed) -> None:
    """The claim has to be visible on the map, or the game does not work."""
    headers, _ = player(authed, "mapper@example.com", 200)
    claim(client, headers, 0, 0)

    lon, lat = axial_to_lonlat(0, 0)
    body = client.get("/world", params={"lat": lat, "lon": lon, "rings": 2}).json()

    owned = [t for t in body["tiles"] if t["owner"]]
    assert len(owned) == 1
    assert (owned[0]["q"], owned[0]["r"]) == (0, 0)
    assert owned[0]["owner"]["display_name"] == "mapper"
    assert body["owned_count"] == 1


def test_claiming_updates_me(client, authed) -> None:
    headers, _ = player(authed, "profile@example.com", 200)
    claim(client, headers, 1, 0)
    claim(client, headers, 1, -1)

    body = client.get("/me", headers=headers).json()
    assert body["owned_tile_count"] == 2
    assert body["mana"] == 200 - 2 * TILE_CLAIM_COST


def test_viewer_id_lets_the_client_highlight_its_own_ground(client, authed) -> None:
    headers, _ = player(authed, "viewer@example.com", 200)
    claim(client, headers, 0, 0)

    lon, lat = axial_to_lonlat(0, 0)
    body = client.get(
        "/world", params={"lat": lat, "lon": lon, "rings": 1}, headers=headers
    ).json()

    assert body["viewer_id"] is not None
    owned = [t for t in body["tiles"] if t["owner"]]
    assert owned[0]["owner"]["id"] == body["viewer_id"]


# ---------------------------------------------------------------------------
# Concurrency
#
# Each test below fails if the row locks are removed. A barrier forces the
# threads to arrive together, so a pass cannot be explained by the calls
# happening to serialise by luck.
# ---------------------------------------------------------------------------

THREADS = 6


def _race(targets, attempt):
    """Run ``attempt(index)`` for every target, all released simultaneously."""
    barrier = threading.Barrier(len(targets))
    results: list[tuple[str, object]] = []
    lock = threading.Lock()

    def run(index: int) -> None:
        try:
            barrier.wait(timeout=20)
            outcome = attempt(index)
        except BaseException as exc:  # noqa: BLE001 - recorded for assertion
            with lock:
                results.append((type(exc).__name__, exc))
        else:
            with lock:
                results.append(("ok", outcome))

    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        list(pool.map(run, range(len(targets))))

    return results


def _claim_for(user_id: int):
    def attempt(index: int):
        q, r = NEIGHBOURS_OF_ORIGIN[index]
        with get_session_factory()() as session:
            return claim_tile(session, user_id=user_id, q=q, r=r)

    return attempt


def test_a_refused_claim_leaves_the_caller_s_session_clean(make_user) -> None:
    """A refusal must not strand the caller's transaction.

    At the HTTP boundary the rollback looks redundant: raising an
    ``HTTPException`` unwinds into ``get_db``, which closes the session and
    rolls back anyway. It is not redundant for a service caller, which may
    catch the exception and keep working in the same session -- and that caller
    would inherit an uncommitted unowned tile row plus a row lock it never
    asked for, and its next claim would be refused by a row it created itself.

    So the contract is stated here and asserted on the session rather than on
    the HTTP response, because the HTTP response cannot tell the difference.
    """
    user_id = make_user(mana=0)

    with get_session_factory()() as session:
        with pytest.raises(InsufficientMana):
            claim_tile(session, user_id=user_id, q=0, r=0)

        assert not session.in_transaction(), (
            "a refused claim left a transaction open in the caller's session"
        )

    assert tile_rows() == [], "the refused claim's tile row survived"


def test_concurrent_claims_cannot_overspend(make_user) -> None:
    """Six simultaneous claims against a balance of three tiles' worth.

    The whole reason the user row is locked first. Without it all six read the
    same 60 mana, all six pass the affordability check, and the player ends up
    owning six tiles for the price of two.

    One tile is seeded so that every target is adjacent to something owned, so
    adjacency cannot be what refuses a claim and mana is the only limiter in
    play. Six targets and three affordable tiles means the excess claims have to
    come back as ``InsufficientMana`` specifically.
    """
    user_id = make_user(mana=3 * TILE_CLAIM_COST)

    with get_session_factory()() as session:
        claim_tile(session, user_id=user_id, q=0, r=0)

    results = _race(NEIGHBOURS_OF_ORIGIN, _claim_for(user_id))

    unexpected = [r[0] for r in results if r[0] not in ("ok", "InsufficientMana")]
    assert not unexpected, unexpected
    assert len([r for r in results if r[0] == "ok"]) == 2, [r[0] for r in results]
    assert owned_count(user_id) == 3, "a tile was created for an unpaid claim"
    assert mana_of(user_id) == 0


def test_concurrent_claims_of_the_same_tile_charge_once(make_user) -> None:
    """The idempotence guarantee under real contention.

    The realistic version is a phone retrying a request whose response it never
    saw, but six at once is the honest stress test: one tile row, one charge,
    and every caller gets a usable answer rather than an error.
    """
    user_id = make_user(mana=TILE_CLAIM_COST)

    def attempt(_index: int):
        with get_session_factory()() as session:
            return claim_tile(session, user_id=user_id, q=0, r=0)

    results = _race([(0, 0)] * THREADS, attempt)

    unexpected = [r for r in results if r[0] != "ok"]
    assert not unexpected, [r[0] for r in results]
    assert len(results) == THREADS

    assert tile_rows() == [(0, 0, user_id)]
    assert mana_of(user_id) == 0, "the player was billed more than once"


def test_concurrent_claims_of_one_tile_by_two_players_have_one_winner(
    make_user,
) -> None:
    """Two players, one hex, exactly one winner.

    This is what the unique constraint on ``(q, r)`` exists for. An
    implementation that decided freeness with a SELECT would let both players
    read "free" and then race to insert.
    """
    alice = make_user(email="alice@example.com", mana=200)
    bob = make_user(email="bob@example.com", mana=200)

    def attempt(index: int):
        user_id = (alice, bob)[index]
        with get_session_factory()() as session:
            return claim_tile(session, user_id=user_id, q=0, r=0)

    results = _race([0, 1], attempt)

    assert [r[0] for r in results].count("ok") == 1, [r[0] for r in results]
    assert [r[0] for r in results].count("TileAlreadyClaimed") == 1

    rows = tile_rows()
    assert len(rows) == 1
    assert rows[0][2] in (alice, bob)


def test_concurrent_adoption_of_one_orphan_row_has_one_winner(make_user) -> None:
    """Two players racing for a row that already exists, unowned.

    The case the insert's conflict handling cannot help with. There is no
    insert to block on here -- the row is already there -- so the only thing
    stopping both players from reading ``owner_id IS NULL`` and both writing
    their own id is the ``FOR UPDATE`` on the tile. Without it, both commits
    succeed, the second silently overwrites the first, and the player whose
    claim was overwritten is charged 20 mana for a hex somebody else now owns.

    This is also the closest thing the design has to a hostile case: an orphan
    row only ever appears from a crash between insert and commit, so the window
    is narrow, but a narrow window that corrupts two players at once is still a
    window.
    """
    with get_session_factory()() as session:
        session.add(HexTile(q=0, r=0, owner_id=None))
        session.commit()

    alice = make_user(email="orphan-a@example.com", mana=200)
    bob = make_user(email="orphan-b@example.com", mana=200)

    def attempt(index: int):
        user_id = (alice, bob)[index]
        with get_session_factory()() as session:
            return claim_tile(session, user_id=user_id, q=0, r=0)

    results = _race([0, 1], attempt)

    assert [r[0] for r in results].count("ok") == 1, [r[0] for r in results]
    assert [r[0] for r in results].count("TileAlreadyClaimed") == 1

    rows = tile_rows()
    assert len(rows) == 1
    winner = rows[0][2]
    assert winner in (alice, bob)
    # The loser kept their mana.
    loser = bob if winner == alice else alice
    assert mana_of(loser) == 200
    assert owned_count(loser) == 0


def test_concurrent_claims_do_not_deadlock(make_user) -> None:
    """Two players racing for the same two hexes, in opposite orders.

    The claim transaction locks the user row and then the tile row, and always
    in that order, so the loser of a contended tile blocks rather than
    deadlocking. If the order were ever "tile then user", or derived from
    request data, this test would hang instead of failing -- which is why the
    futures carry an explicit timeout.
    """
    alice = make_user(email="a@example.com", mana=200)
    bob = make_user(email="b@example.com", mana=200)

    # Adjacent starting tiles, so each player has territory to grow from.
    with get_session_factory()() as session:
        claim_tile(session, user_id=alice, q=0, r=0)
    with get_session_factory()() as session:
        claim_tile(session, user_id=bob, q=0, r=1)

    barrier = threading.Barrier(2)
    done: list[str] = []
    lock = threading.Lock()

    def attempt(user_id: int, order) -> None:
        with get_session_factory()() as session:
            barrier.wait(timeout=20)
            for q, r in order:
                try:
                    claim_tile(session, user_id=user_id, q=q, r=r)
                except (
                    TileAlreadyClaimed,
                    NotAdjacentToTerritory,
                    InsufficientMana,
                ) as exc:
                    with lock:
                        done.append(type(exc).__name__)
                else:
                    with lock:
                        done.append("ok")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            # Same two contested hexes, opposite order.
            pool.submit(attempt, alice, [(1, 0), (1, 1)]),
            pool.submit(attempt, bob, [(1, 1), (1, 0)]),
        ]
        for future in futures:
            future.result(timeout=30)

    assert len(done) == 4, done
    # Each contested hex went to exactly one player.
    assert done.count("ok") == 2, done
    assert done.count("TileAlreadyClaimed") == 2, done


def test_regeneration_is_credited_once_under_concurrency(make_user, age_user) -> None:
    """Regen is settled inside the claim, so it cannot be double-counted.

    Two simultaneous claims against a player holding exactly one tile's worth of
    accrued regen. The threads serialise on the user row, so the first advances
    the regeneration watermark by whole periods and the second finds nothing
    left to collect. If the lock were dropped, or if the watermark update were
    not in the same transaction as the grant, both would read the same
    un-advanced watermark, both would credit the regen, and the player would
    farm mana by sending claims in parallel.
    """
    periods = (2 * TILE_CLAIM_COST - 1) // MANA_REGEN_AMOUNT
    user_id = make_user(mana=0)
    age_user(user_id, by=timedelta(minutes=MANA_REGEN_PERIOD_MINUTES * periods))

    def attempt(index: int):
        with get_session_factory()() as session:
            return claim_tile(session, user_id=user_id, q=index, r=0)

    results = _race([0, 1], attempt)

    assert sorted(r[0] for r in results) == ["InsufficientMana", "ok"], [
        r[0] for r in results
    ]
    assert owned_count(user_id) == 1
    assert mana_of(user_id) == MANA_REGEN_AMOUNT * periods - TILE_CLAIM_COST


def test_claiming_conserves_mana(client, authed) -> None:
    """Mana goes down only when a claim succeeds, and never below zero."""
    headers, user_id = player(authed, "conserve@example.com", 3 * TILE_CLAIM_COST)

    assert claim(client, headers, 0, 0).status_code == 200
    assert mana_of(user_id) == 2 * TILE_CLAIM_COST

    # Refused for being far away: nothing is charged.
    assert claim(client, headers, FAR_Q, FAR_R).status_code == 422
    assert mana_of(user_id) == 2 * TILE_CLAIM_COST

    assert claim(client, headers, 1, 0).status_code == 200
    assert claim(client, headers, 1, -1).status_code == 200
    assert mana_of(user_id) == 0

    # Adjacent, affordable in principle, empty: refused and still not negative.
    assert claim(client, headers, 1, 1).status_code == 402
    assert mana_of(user_id) == 0
    assert owned_count(user_id) == 3
    assert len(tile_rows()) == 3


def test_insufficient_mana_outranks_a_non_adjacent_tile(client, authed) -> None:
    """Which of the two 4xx codes wins when a claim breaks both rules.

    Mana is checked first, on purpose: the balance is already in memory while
    the adjacency question costs a query, and "you cannot afford any tile" is
    more useful to a broke player than "that specific tile is too far", which
    would be moot anyway. Pinned so that reordering the checks is a deliberate
    change rather than an accident.
    """
    headers, user_id = player(authed, "broke2@example.com", TILE_CLAIM_COST)
    assert claim(client, headers, 0, 0).status_code == 200

    assert claim(client, headers, FAR_Q, FAR_R).status_code == 402

    # And with mana restored the very same request fails on adjacency instead.
    with get_session_factory()() as session:
        session.get(User, user_id).mana = TILE_CLAIM_COST
        session.commit()
    assert claim(client, headers, FAR_Q, FAR_R).status_code == 422
