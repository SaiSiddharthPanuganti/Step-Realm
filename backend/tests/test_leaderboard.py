"""Tests for the leaderboard (phase 8).

The board is a list of players ranked by territory, served from the Redis
sorted set with a Postgres recompute as the fallback. The three things that
need pinning are the ranking rules (ties share a rank), the isolation story
Redis is allowed to skip (it is a cache), and the fact that a claim lands the
claimer on the board the moment it commits.
"""

from __future__ import annotations

import pytest

from app.models.hex_tile import HexTile
from app.services import leaderboard_service
from app.services.leaderboard_service import LEADERBOARD_KEY

EMAIL = "leader@example.com"
PASSWORD = "correct-horse-battery-staple"


def _award_tiles(user_id: int, tiles: int) -> None:
    """Give a player ``tiles`` hexes, bypassing the claim path.

    Territorial rows created directly so the ranking logic is exercised
    without riding the claim endpoint's walk-and-spend flow; the endpoint's
    own contribution (the sync write) is covered by its dedicated test.

    Coordinates are scoped to the user id so two players in one test cannot
    collide on ``uq_hex_tiles_q_r``.
    """
    from app.db.session import get_session_factory

    with get_session_factory()() as session:
        for index in range(tiles):
            session.add(
                HexTile(
                    q=user_id * 1000 + index, r=0, owner_id=user_id
                )
            )
        session.commit()


def _signup(client, email: str = EMAIL) -> dict:
    response = client.post("/auth/signup", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()


def _board(client, headers: dict, **params) -> dict:
    response = client.get("/leaderboard", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def test_ranks_by_territory_and_shares_ties(client, make_user) -> None:
    alice = make_user("alice@x.com", mana=60)
    bella = make_user("bella@x.com", mana=10)
    cara = make_user("cara@x.com", mana=0)
    _award_tiles(alice, 2)
    _award_tiles(bella, 2)
    _award_tiles(cara, 1)

    board = _board(client, _signup_head(client), limit=10)

    by_id = {entry["user_id"]: entry for entry in board["entries"]}
    assert [entry["owned_tile_count"] for entry in board["entries"]] == [2, 2, 1]
    # Competition ranking: the two 2-tile players share first, the player with
    # one tile is third -- the second position is not granted by default.
    assert by_id[alice]["rank"] == 1
    assert by_id[bella]["rank"] == 1
    assert by_id[cara]["rank"] == 3
    # Display name and balance ride along for the client to render.
    assert by_id[alice]["display_name"] is not None
    assert by_id[alice]["mana"] == 60


def test_viewer_is_included_beyond_the_page_with_their_global_rank(
    client, make_user
) -> None:
    alice = make_user("alice@x.com")
    bella = make_user("bella@x.com")
    _award_tiles(alice, 3)
    _award_tiles(bella, 2)
    viewer = _signup(client)

    board = _board(client, _token_headers(viewer), limit=1)

    assert len(board["entries"]) == 1
    assert board["entries"][0]["owned_tile_count"] == 3
    # The viewer owns nothing, so their rank is None ("unranked", not "last").
    assert board["viewer"] is not None
    assert board["viewer"]["user_id"] == viewer["user"]["id"]
    assert board["viewer"]["owned_tile_count"] == 0
    assert board["viewer"]["rank"] is None


def test_viewer_with_territory_gets_a_rank_including_those_ahead(
    client, make_user
) -> None:
    winner = make_user("win@x.com")
    viewer = _signup(client)
    viewer_id = viewer["user"]["id"]
    _award_tiles(winner, 3)
    _award_tiles(viewer_id, 2)

    board = _board(client, _token_headers(viewer), limit=1)

    # Only "win@x.com" has more territory than the viewer, so the viewer is
    # second even though the page of one does not show them.
    assert board["viewer"]["rank"] == 2


def test_an_empty_world_has_no_entries_but_still_the_viewer(
    client, make_user
) -> None:
    viewer = _signup(client)

    board = _board(client, _token_headers(viewer))

    assert board["entries"] == []
    assert board["viewer"]["user_id"] == viewer["user"]["id"]


def test_page_limit_is_bounded(client, make_user) -> None:
    for index in range(5):
        uid = make_user(f"p{index}@x.com")
        _award_tiles(uid, 1)
    headers = _signup_head(client)

    small = _board(client, headers, limit=2)
    assert len(small["entries"]) == 2
    assert small["limit"] == 2

    assert client.get("/leaderboard", headers=headers, params={"limit": 0}).status_code == 422
    assert client.get("/leaderboard", headers=headers, params={"limit": 1001}).status_code == 422


# ---------------------------------------------------------------------------
# Redis behaviour
# ---------------------------------------------------------------------------


def test_a_claim_syncs_the_score_and_lands_the_claimer_on_the_board(client) -> None:
    claimer = _signup(client)
    headers = _token_headers(claimer)

    world = client.get("/world", params={"lat": 12.9716, "lon": 77.5946, "rings": 1})
    assert world.status_code == 200
    target = next(tile for tile in world.json()["tiles"] if tile["owner"] is None)

    walked = client.post(
        "/territory/walk",
        headers=headers,
        json={"lat": target["lat"], "lon": target["lon"], "meters": 20},
    )
    assert walked.status_code == 200, walked.text

    claimed = client.post(
        "/territory/claim", headers=headers, json={"q": target["q"], "r": target["r"]}
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["owned_tile_count"] == 1

    # The best-effort cache write actually landed, and a rival's view of the
    # board shows the new territory at the top.
    from app.db.redis import get_redis as _get_redis

    assert float(_get_redis().zscore(LEADERBOARD_KEY, str(claimer["user"]["id"]))) == 1.0

    rival = _signup(client, "rival@x.com")
    board = _board(client, _token_headers(rival))
    assert board["entries"][0]["user_id"] == claimer["user"]["id"]
    assert board["entries"][0]["owned_tile_count"] == 1


def test_a_stale_redis_score_never_outranks_the_database(client, make_user) -> None:
    """The board counts tiles from Postgres, not from whatever the cache last wrote.

    Redis is fed by one best-effort write per claim, so a score can drift away
    from the database and stay there: the only reconciliation ran when the whole
    set was empty, so a member that went stale kept its number for the life of
    the process. That is not hypothetical bookkeeping -- it is what a player
    sees when the board and the map disagree about the same ground, because the
    map reads the database and this screen read the cache. A ranking built on a
    cached number is a ranking nobody can act on, so the database decides both
    the count and the order, and the cache is repaired from it.
    """
    from app.db.redis import get_redis

    alice = make_user("alice@x.com")
    bella = make_user("bella@x.com")
    _award_tiles(alice, 1)
    _award_tiles(bella, 3)
    redis = get_redis()
    # Both players claimed, so both are in the cache with the counts they had.
    redis.zadd(LEADERBOARD_KEY, {str(alice): 1, str(bella): 3})
    # Then the ground alice holds is gone and her score is not.
    from app.db.session import get_session_factory

    with get_session_factory()() as session:
        session.query(HexTile).filter(HexTile.owner_id == alice).delete()
        session.commit()
    redis.zadd(LEADERBOARD_KEY, {str(alice): 9})

    viewer = _signup(client, "viewer3@x.com")
    board = _board(client, _token_headers(viewer), limit=10)

    by_id = {entry["user_id"]: entry for entry in board["entries"]}
    # Alice holds nothing, so she does not rank -- and her cache member is gone,
    # rather than left behind to resurrect a phantom next time the set is read.
    assert [entry["user_id"] for entry in board["entries"]] == [bella]
    assert by_id[bella]["owned_tile_count"] == 3
    assert by_id[bella]["rank"] == 1
    assert redis.zscore(LEADERBOARD_KEY, str(alice)) is None
    assert redis.zscore(LEADERBOARD_KEY, str(bella)) == 3.0


def test_a_stale_score_is_corrected_rather_than_dropped(client, make_user) -> None:
    """A score that is merely wrong is rewritten, so the player keeps their rank.

    The companion to the test above: a player whose count drifted *down* is
    still on the board, just not where the cache said. Dropping them would hide
    a real player; leaving them would rank them by a number that is not theirs.
    """
    from app.db.redis import get_redis

    alice = make_user("alice@x.com")
    bella = make_user("bella@x.com")
    _award_tiles(alice, 4)
    _award_tiles(bella, 3)
    redis = get_redis()
    # alice grew from one tile to four; only the cache still remembers one.
    redis.zadd(LEADERBOARD_KEY, {str(alice): 1, str(bella): 3})

    viewer = _signup(client, "viewer4@x.com")
    board = _board(client, _token_headers(viewer), limit=10)

    by_id = {entry["user_id"]: entry for entry in board["entries"]}
    assert by_id[alice]["owned_tile_count"] == 4
    # Four beats three, so the cache's ordering was wrong as well as its number.
    assert [entry["user_id"] for entry in board["entries"]] == [alice, bella]
    assert by_id[alice]["rank"] == 1
    assert by_id[bella]["rank"] == 2
    assert redis.zscore(LEADERBOARD_KEY, str(alice)) == 4.0


def test_a_cold_redis_set_is_rebuilt_from_postgres(client, make_user) -> None:
    from app.db.redis import get_redis

    holder = make_user("holder@x.com", mana=5)
    _award_tiles(holder, 3)
    redis = get_redis()
    # Whatever is in the sorted set, wipe it -- the cold-start state we are
    # simulating is "Redis restarted and remembered nothing". The return value
    # is 1 when something was deleted and 0 when the set was already empty,
    # and both are valid here.
    redis.delete(LEADERBOARD_KEY)

    viewer = _signup(client, "viewer@x.com")
    board = _board(client, _token_headers(viewer), limit=10)

    assert board["degraded"] is False
    assert board["entries"][0]["user_id"] == holder
    assert board["entries"][0]["owned_tile_count"] == 3


def test_degraded_when_redis_is_unconfigured(
    client, make_user, settings_override
) -> None:
    from app.db import redis as redis_module

    holder = make_user("holder2@x.com", mana=5)
    _award_tiles(holder, 2)

    # An *empty* string, not an unset variable: the backend reads Redis from
    # dotenv as well as the environment, so merely deleting the env var would
    # fall through to backend/.env and Redis would be configured again.
    settings_override(REDIS_URL="")
    redis_module.reset_redis_client()

    viewer = _signup(client, "viewer2@x.com")
    board = _board(client, _token_headers(viewer))

    # The board is still correct -- Postgres is the authority -- but honest
    # about being served without Redis.
    assert board["degraded"] is True
    assert board["entries"][0]["user_id"] == holder


def test_requires_a_token(client) -> None:
    response = client.get("/leaderboard")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signup_head(client) -> dict:
    return _token_headers(_signup(client))


def _token_headers(body: dict) -> dict:
    return {"Authorization": f"Bearer {body['access_token']}"}