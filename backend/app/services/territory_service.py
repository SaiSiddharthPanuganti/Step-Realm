"""Claiming territory: the one transaction that spends mana and takes a hex.

Everything about this module is about concurrency, because the game has no
other interesting failure mode. A claim does three things that must not
interleave: it reads a mana balance, it decides whether a hex is free, and it
writes both. The naive version -- read mana, check the tile, insert, deduct --
loses mana and duplicates tiles under exactly the conditions the game creates
most often, which is two phones claiming at once.

The design follows from one rule: **lock in a fixed order, and let the
database decide the rest.**

* The user row is locked first, via the same
  :func:`app.services.mana_service.load_user_for_update` that ``/mana/sync`` and
  ``GET /me`` already use. Reusing it is not tidiness -- it means the mana read
  here is serialised against every other path that can change a balance, and
  that the stale instance handed over by ``get_current_user`` is discarded.
* The tile row is locked second. User-then-tile is the order every claim takes,
  so two claims can block each other but can never deadlock.
* Whether a hex is free is decided by the unique constraint on ``(q, r)``, not
  by a SELECT. A pre-check cannot make two concurrent claims of the same hex
  safe no matter how careful it looks, because both readers see "free" before
  either writes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import (
    COORDINATE_PRECISION,
    COORDINATE_PRECISION_DEGREES,
    HEX_NEIGHBOR_OFFSETS,
    TILE_CLAIM_COST,
)
from app.models.hex_tile import HexTile
from app.services import mana_service
from app.services.geo import (
    axial_to_lonlat,
    axial_to_world,
    hex_distance,
    lonlat_to_axial,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain errors
#
# Each maps to a distinct HTTP status so the client can branch on the status
# alone. The three are genuinely different player situations and must not be
# collapsed: "someone beat you to it" means stop, "go for a walk" means come
# back later, and "claim a neighbour" means pick a different hex.
# ---------------------------------------------------------------------------


class TileAlreadyClaimed(Exception):
    """The hex belongs to another player."""


class InsufficientMana(Exception):
    """The player cannot afford the claim.

    Carries the numbers so the API can tell the player how far away they are
    rather than just refusing.
    """

    def __init__(self, available: int, required: int) -> None:
        super().__init__(
            f"claim costs {required} mana, you have {available}"
        )
        self.available = available
        self.required = required


class NotAdjacentToTerritory(Exception):
    """The hex is free but does not touch anything the player owns.

    The exception the spec's "tile exists in the generated world" precondition
    degrades to against an unbounded plane. See
    :func:`check_world_leash` for the other half of that degradation: the
    configured arena boundary.
    """


class OutsideWorldLeash(Exception):
    """The hex is beyond the configured radius from the world origin."""


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    """What a claim did, for the response builder and the tests."""

    q: int
    r: int
    world_x: float
    world_y: float
    lat: float
    lon: float
    is_new: bool
    mana_spent: int
    mana: int
    owned_tile_count: int
    mana_gained_since_last_read: int
    """Passive regeneration applied during this claim. Not lost -- it is already
    in ``mana`` -- but reported so a player who suddenly affords a tile after a
    long break is not left wondering where the mana came from."""


# ---------------------------------------------------------------------------
# Tile row acquisition
# ---------------------------------------------------------------------------


def _lock_tile_row(session: Session, q: int, r: int) -> HexTile:
    """Materialise the tile if it does not exist, then lock it. Commits nothing.

    The insert-then-read shape is deliberate, and the two halves do different
    jobs. ``SELECT ... FOR UPDATE`` on a row that does not exist locks nothing
    at all, so it cannot be the first step: two claims of a never-claimed hex
    would both find nothing and neither would be protected.

    * The **insert** is what serialises the never-claimed case. Resolving an
      ``ON CONFLICT`` against the unique index means the loser's insert waits
      for the winner's transaction to finish, so the read below is guaranteed to
      see a committed row rather than an empty gap. Mutating the lock away from
      the read leaves the never-claimed race passing, because the insert is
      already doing that work.
    * The **lock** is what serialises the row-already-exists case, which the
      insert cannot help with because there is no insert to block on: a row left
      unowned by an earlier crash. Without it, two players both read
      ``owner_id IS NULL``, both assign their own id, and the second commit
      silently overwrites the first while charging them for a hex they lost.
      It also pins the row for the rest of the transaction, so the owner read
      here is the owner that is committed against.

    ``owner_id`` is inserted as NULL and assigned before the commit, so a
    committed row is always owned. That is why the column is nullable despite
    the domain saying "a row exists iff claimed" -- and why the assignment and
    the commit happen in the same transaction, with a rollback between them
    never happening.
    """
    session.execute(
        pg_insert(HexTile)
        .values(q=q, r=r, owner_id=None)
        .on_conflict_do_nothing(index_elements=["q", "r"])
    )

    tile = session.execute(
        select(HexTile)
        .where(HexTile.q == q, HexTile.r == r)
        .with_for_update(of=HexTile)
        .execution_options(populate_existing=True)
    ).scalar_one()
    return tile


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


def count_owned_tiles(session: Session, user_id: int) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(HexTile)
            .where(HexTile.owner_id == user_id)
        )
        or 0
    )


def touches_owned_territory(session: Session, user_id: int, q: int, r: int) -> bool:
    """Is ``(q, r)`` edge-adjacent to something this player owns?

    Six equality tests against the one index-backed predicate rather than a
    distance calculation over the player's whole territory. ``hex_distance``
    would also be correct but needs every owned tile in Python, which turns
    each claim into O(territory size); this stays O(1) no matter how large a
    player's holdings get.
    """
    conditions = or_(
        *(
            (HexTile.q == q + dq) & (HexTile.r == r + dr)
            for dq, dr in HEX_NEIGHBOR_OFFSETS
        )
    )
    found = session.scalar(
        select(func.count())
        .select_from(HexTile)
        .where(HexTile.owner_id == user_id, conditions)
    )
    return bool(found)


def check_world_leash(q: int, r: int) -> None:
    """Enforce ``config.world_max_radius_from_origin`` when it is configured.

    Inert for the MVP, where the setting is ``None`` and the plane is unbounded.
    Written anyway, and applied to *every* claim including a player's first, so
    that the constant is not a lie: someone setting the option gets a bounded
    arena -- including the anti-cheat hole it exists to close -- without
    discovering that first tiles are exempt.
    """
    settings = get_settings()
    max_radius = settings.world_max_radius_from_origin
    if max_radius is None:
        return

    origin_q, origin_r = lonlat_to_axial(
        settings.world_origin_lat, settings.world_origin_lon
    )
    if hex_distance((origin_q, origin_r), (q, r)) > max_radius:
        raise OutsideWorldLeash


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------


def claim_tile(
    session: Session,
    *,
    user_id: int,
    q: int,
    r: int,
    now=None,
) -> ClaimOutcome:
    """Claim the hex at ``(q, r)`` for ``user_id``, or raise.

    Idempotent for a tile the caller already owns: it reports success and
    charges nothing. That is a deliberate choice over a 409, because the
    realistic caller is a phone that sent a claim, lost the response, and
    retried. Under a 409 the player would see an error for something they
    genuinely own and could not tell it apart from losing the hex to somebody
    else.
    """
    # 1. Lock the user, first and always in this order. settle_mana here is
    #    what makes the claim affordable off regen alone, and what rolls the
    #    daily reset if the player is claiming after UTC midnight.
    user = mana_service.load_user_for_update(session, user_id)
    mana_outcome = mana_service.settle_mana(user, steps=0, now=now)
    regen = mana_outcome.passive_gained

    # 2. Lock (or create) the tile.
    tile = _lock_tile_row(session, q, r)

    # 3. Decide, in an order chosen so that the *cheap* facts are checked
    #    before the ones that cost a query, and so that the answer does not
    #    depend on which condition happens to fail first.
    already_mine = tile.owner_id == user_id
    taken_by_other = tile.owner_id is not None and not already_mine

    if taken_by_other:
        session.rollback()
        logger.info("claim rejected: tile already held", extra={"q": q, "r": r})
        raise TileAlreadyClaimed

    if not already_mine:
        # Mana before adjacency: the balance is already in memory, so this
        # costs nothing, whereas the adjacency question needs a round trip and
        # is better advice to give only to a player who can actually pay.
        #
        # Read the balance into a local *before* rolling back. ``rollback()``
        # expires every instance in the session, so the next attribute access
        # re-reads the row -- and gets the value from before this transaction,
        # which is a balance with none of the regen just settled in step 1.
        # The claim is still refused either way, but the player would be told
        # "you have 0" while sitting on 10, and the only way to make that
        # number true is to go and earn mana they have already earned.
        available = user.mana
        if available < TILE_CLAIM_COST:
            session.rollback()
            raise InsufficientMana(available, TILE_CLAIM_COST)

        owned = count_owned_tiles(session, user_id)
        if owned > 0 and not touches_owned_territory(session, user_id, q, r):
            # A player's very first tile is unrestricted, which is the spec's
            # known and accepted anti-cheat hole: a spoofed GPS can seed a
            # territory anywhere on Earth. Every tile after that must touch
            # what they hold, so the map cannot be seeded arbitrarily far apart
            # or carpeted in disconnected pockets.
            session.rollback()
            raise NotAdjacentToTerritory

        check_world_leash(q, r)

        tile.owner_id = user_id
        user.mana -= TILE_CLAIM_COST
        mana_spent = TILE_CLAIM_COST
        is_new = True
    else:
        mana_spent = 0
        is_new = False

    # Explicit flush, and the project sets ``autoflush=False`` on the session
    # factory so that flush points are deliberate rather than implicit in every
    # query. Without this the COUNT below would not see the ownership change
    # made a line earlier and the response would report ``owned_tile_count: 0``
    # to a player who had just claimed a tile -- a bug that only shows up as a
    # wrong number in the client, with the database perfectly correct.
    session.flush()

    owned_count = count_owned_tiles(session, user_id)
    # Same expiry trap as above, benign here but worth being explicit about:
    # after ``commit()`` this attribute would re-read the row for a value the
    # database already holds. Capturing it first documents that the number in
    # the response is the one this transaction decided on.
    mana_after = user.mana
    session.commit()

    world_x, world_y = axial_to_world(q, r)
    lon, lat = axial_to_lonlat(q, r)

    return ClaimOutcome(
        q=q,
        r=r,
        world_x=round(world_x, COORDINATE_PRECISION),
        world_y=round(world_y, COORDINATE_PRECISION),
        lat=round(lat, COORDINATE_PRECISION_DEGREES),
        lon=round(lon, COORDINATE_PRECISION_DEGREES),
        is_new=is_new,
        mana_spent=mana_spent,
        mana=mana_after,
        owned_tile_count=owned_count,
        mana_gained_since_last_read=regen,
    )


def claim_tile_at_location(
    session: Session, *, user_id: int, lat: float, lon: float, now=None
) -> ClaimOutcome:
    """Claim the hex containing ``(lat, lon)``.

    A thin wrapper, and intentionally so: the projection runs exactly once, in
    one place, so this endpoint and ``/territory/claim`` cannot disagree about
    which hex a coordinate belongs to. Latitude beyond the Mercator limit is
    clamped rather than rejected, matching ``GET /world`` and
    :data:`app.core.constants.WORLD_MAX_LATITUDE_MERCATOR`.
    """
    q, r = lonlat_to_axial(lat, lon)
    return claim_tile(session, user_id=user_id, q=q, r=r, now=now)
