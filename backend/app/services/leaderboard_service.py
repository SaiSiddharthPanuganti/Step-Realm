"""Leaderboard: players ranked by tiles claimed, cached in Redis.

Redis in StepRealm is *derived and best-effort* (see app/db/redis.py): it is a
cache of state Postgres owns and can always recompute, and nothing here may
ever raise into a request path. This service is that promise made concrete.

Storage
-------
A single sorted set, ``steprealm:leaderboard:tiles``:

* member: ``user_id`` as text
* score:  ``owned_tile_count`` -- the number of hexes the player has claimed.

The score is *territory*, not mana, and that is a deliberate design call.
Mana is capped at 200 with claims costing 20, so almost every active player
sits at the cap and the board would be a wall of ties. Territory only ties
when two players literally claim the same number of hexes, and the mana
column is still displayed per entry so a tie stays viewable.

Writes are incremental. A successful claim bumps exactly one player with a
single ``ZADD``, safe to drop when Redis is down -- the ranking is a cache and
a dropped write only makes that one entry stale until the next successful
write or rebuild.

Reads
-----
``top_entries`` is the only public read. Redis is preferred, and when it is
unconfigured, unreachable, or cold (empty after a restart -- the dev container
runs ``--save "" --appendonly no``, so an empty set is indistinguishable from
"nobody has claimed anything" and a rebuild is cheap either way), the same
answer is recomputed from Postgres in one grouped query. When the emptiness
was Redis's fault and Redis is reachable, the recompute repopulates the set.

Redis picks the players; Postgres counts their tiles. A sorted set written once
per claim cannot tell that a count stopped being true, and this board ranks on
the count -- so a score that drifted would outrank real territory indefinitely,
and the map (which reads the table) would contradict it. Every read therefore
re-reads the counts for the candidates it is about to show and writes any
correction back, which keeps the cache honest without a reconciliation job.

``degraded`` tells the caller which path served: False means Redis answered,
True means the board came straight from Postgres because Redis could not. The
rankings are identical either way; the flag only exists so a client can
choose to show a "live" or "computed" caption.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.redis import get_redis
from app.models.hex_tile import HexTile
from app.models.user import User
from app.schemas.leaderboard import LeaderboardViewer
from app.services.user_service import count_owned_tiles

logger = logging.getLogger(__name__)

LEADERBOARD_KEY = "steprealm:leaderboard:tiles"
"""The sorted set holding every ranked player's territory count."""


@dataclass(frozen=True, slots=True)
class LeaderboardRow:
    """One ranked player. ``rank`` is competition-style: ties share a number."""

    rank: int
    user_id: int
    display_name: str
    mana: int
    owned_tile_count: int

LEADERBOARD_DEFAULT_LIMIT = 50
LEADERBOARD_MAX_LIMIT = 100
"""Page size bounds, mirrored in the query parameter of ``GET /leaderboard``.

The board is a read a client is likely to refresh while walking, so the page
is deliberately bounded; 100 is more than any MVP ranks worth showing and
still a single round trip.
"""


def _tiles_by_user(
    session: Session, *, limit: int | None = None
) -> list[tuple[int, str, int, int]]:
    """``(user_id, display_name, mana, tiles)`` ordered most territory first.

    The join means only players holding at least one hex appear -- a player
    who owns nothing ranks nowhere. Ties break on user id for a stable order.
    Backed by ``ix_hex_tiles_owner_id``, so at MVP scale the COUNT is cheap.
    """
    statement = (
        select(
            User.id,
            User.display_name,
            User.mana,
            func.count(HexTile.id).label("tiles"),
        )
        .join(HexTile, HexTile.owner_id == User.id)
        .group_by(User.id, User.display_name, User.mana)
        .order_by(func.count(HexTile.id).desc(), User.id.asc())
    )
    if limit is not None:
        statement = statement.limit(limit)
    return [tuple(row) for row in session.execute(statement).all()]  # type: ignore[misc]


def sync_tile_count(user_id: int, owned_tile_count: int) -> bool:
    """Bump one player's score to their territory count. Best effort.

    Called after a successful claim. The score is the final count for that
    claim, so a retried idempotent claim writes the same value and costs
    nothing. Returns whether the write landed, for tests.
    """
    client = get_redis()
    if client is None:
        return False
    try:
        client.zadd(LEADERBOARD_KEY, {str(user_id): owned_tile_count})
        return True
    except Exception:
        logger.warning(
            "leaderboard score sync failed; the board may be stale",
            extra={"user_id": user_id},
            exc_info=True,
        )
        return False


def reset_leaderboard() -> None:
    """Drop the whole sorted set. For tests and ops.

    Idempotent and never raises; a Redis that cannot answer simply leaves the
    set alone, which the next read will rebuild from Postgres anyway.
    """
    client = get_redis()
    if client is None:
        return
    try:
        client.delete(LEADERBOARD_KEY)
    except Exception:
        logger.debug("leaderboard reset failed", exc_info=True)


def _top_from_postgres(
    session: Session, limit: int
) -> list[LeaderboardRow]:
    """Rank the top ``limit`` players from Postgres alone.

    The fallback path: Redis is unconfigured or unreachable, and Postgres is
    the authority, so the board is served straight from the single grouped
    query.
    """
    rows = _tiles_by_user(session, limit=limit)
    entries: list[LeaderboardRow] = []
    last_tiles: int | None = None
    for index, (user_id, display_name, mana, tiles) in enumerate(rows):
        if tiles != last_tiles:
            rank = index + 1
        entries.append(
            LeaderboardRow(
                rank=rank,
                user_id=user_id,
                display_name=display_name,
                mana=mana,
                owned_tile_count=int(tiles),
            )
        )
        last_tiles = tiles
    return entries


def _rebuild_into_redis(session: Session, client) -> None:
    """Repopulate the sorted set from Postgres.

    Only called when the set is cold (empty), so it cannot clobber fresher
    incremental writes. Overwrites whatever it finds with the authoritative
    counts, which is the point: a stale set is emptied by whichever future
    event needs it and rebuilt.
    """
    rows = _tiles_by_user(session)
    if not rows:
        return
    client.zadd(LEADERBOARD_KEY, {str(row[0]): row[3] for row in rows})


def _tile_counts_for(session: Session, user_ids: list[int]) -> dict[int, int]:
    """``user_id -> owned tile count`` for the given players.

    The authoritative count, from the table that owns the fact. One grouped
    query over ``ix_hex_tiles_owner_id``, so asking about a whole page of
    players costs the same as asking about one.
    """
    if not user_ids:
        return {}
    statement = (
        select(HexTile.owner_id, func.count(HexTile.id))
        .where(HexTile.owner_id.in_(user_ids))
        .group_by(HexTile.owner_id)
    )
    return {owner_id: int(count) for owner_id, count in session.execute(statement)}


def _repair_scores(client, counts: dict[int, int], observed: dict[int, int]) -> None:
    """Write the database's counts back over the cache's, and drop empties.

    Self-healing rather than scheduled: a read is the only moment we know a
    score was wrong, so that is where the correction belongs. Two rules, both
    minimal writes -- only a member whose score actually differs is touched, and
    a player holding no ground is removed rather than parked at zero, because a
    territory board has no row for somebody with no territory.
    """
    stale = {str(user_id): count for user_id, count in counts.items() if observed.get(user_id) != count}
    if stale:
        client.zadd(LEADERBOARD_KEY, stale)
    empty = [str(user_id) for user_id in observed if user_id not in counts]
    if empty:
        client.zrem(LEADERBOARD_KEY, *empty)


def _top_from_redis(session: Session, client, limit: int) -> list[LeaderboardRow]:
    """Rank the top ``limit`` entries.

    Redis supplies the *candidates* -- which players are worth ranking at all --
    and Postgres supplies the numbers. That split is the whole point, and it is
    not a compromise: a sorted set fed by one best-effort write per claim has
    no way to notice that a count stopped being true, and a board that ranks on
    a stale number is a board a player cannot act on. The detail fields already
    needed a Postgres read, so the count rides along in the same round trip and
    the ordering follows the count rather than the cache.

    Raises on any Redis failure so the caller can fall back.
    """
    if client.zcard(LEADERBOARD_KEY) == 0:
        _rebuild_into_redis(session, client)

    raw = client.zrevrange(LEADERBOARD_KEY, 0, limit - 1, withscores=True)
    if not raw:
        return []

    ids = [int(member) for member, _score in raw]
    users = {
        user.id: user
        for user in session.scalars(select(User).where(User.id.in_(ids)))
    }
    counts = _tile_counts_for(session, ids)

    # Best effort, and deliberately not on the critical path: a cache that
    # refuses to be corrected costs one wrong read, not a wrong board.
    _repair_scores(client, counts, {int(member): int(score) for member, score in raw})

    # Same order as the Postgres path -- territory first, then user id so ties
    # do not shuffle between the two ways of answering the same question.
    ordered = sorted(
        ((user_id, count) for user_id, count in counts.items() if user_id in users),
        key=lambda pair: (-pair[1], pair[0]),
    )[:limit]

    entries: list[LeaderboardRow] = []
    last_tiles: int | None = None
    for index, (user_id, tiles) in enumerate(ordered):
        if tiles != last_tiles:
            rank = index + 1
        entries.append(
            LeaderboardRow(
                rank=rank,
                user_id=user_id,
                display_name=users[user_id].display_name,
                mana=users[user_id].mana,
                owned_tile_count=tiles,
            )
        )
        last_tiles = tiles
    return entries


def top_entries(session: Session, limit: int) -> tuple[list[LeaderboardRow], bool]:
    """The top ``limit`` entries, and whether Redis served them.

    Returns ``(entries, degraded)``. Degraded is True only when Redis could
    not help and the board was computed from Postgres.
    """
    client = get_redis()
    if client is not None:
        try:
            return _top_from_redis(session, client, limit), False
        except Exception:
            logger.warning("leaderboard read could not use Redis", exc_info=True)
    return _top_from_postgres(session, limit), True


def viewer_entry(
    session: Session, user: User, *, mana: int | None = None
) -> LeaderboardViewer:
    """The caller's own row, so a player always sees where they stand.

    Included whether or not the player cracked the top ``limit``. Rank is the
    player's global standing -- one better than the number of players with
    strictly more territory -- and is None while they own nothing, because
    "unranked" is a more honest position than "last".
    """
    owned = count_owned_tiles(session, user.id)
    if owned == 0:
        rank = None
    else:
        ahead = (
            select(HexTile.owner_id)
            .join(User, User.id == HexTile.owner_id)
            .group_by(HexTile.owner_id)
            .having(func.count(HexTile.id) > owned)
            .subquery()
        )
        rank = int(session.scalar(select(func.count()).select_from(ahead)) or 0) + 1
    return LeaderboardViewer(
        user_id=user.id,
        display_name=user.display_name,
        mana=mana if mana is not None else user.mana,
        owned_tile_count=owned,
        rank=rank,
    )