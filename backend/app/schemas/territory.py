"""Wire models for claiming territory.

Two request shapes, one response, because the two are genuinely different
operations from the client's point of view. ``claim`` takes the grid
coordinates the client already has from ``GET /world`` and is what a tap on a
hex sends. ``claim_by_location`` takes a raw GPS fix and is what a "claim the
ground I'm standing on" button sends; the server does the projection, so the
client never has to agree with the backend about which hex a coordinate falls
in. Both funnel into the same service call, so the rules cannot drift apart
between them.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.constants import TILE_CLAIM_COST


class ClaimRequest(BaseModel):
    """Claim a specific hex by its axial coordinates."""

    q: int
    r: int
    """Axial, matching ``GET /world`` and the ``hex_tiles`` table exactly.

    Deliberately not a lat/lon pair: two clients that each convert a coordinate
    to a hex can disagree, and only one of them can be right. The authoritative
    conversion lives in ``app.services.geo`` and is used by both this
    endpoint and the claim-by-location one.
    """


class ClaimByLocationRequest(BaseModel):
    """Claim whichever hex contains this point.

    Ranges are the full valid domain rather than the Mercator limit, so that a
    GPS fix inside the projection's dead zone is clamped and reported the same
    way ``GET /world`` handles it, instead of being rejected here and accepted
    there.
    """

    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)


class ClaimedTile(BaseModel):
    """The tile as it now stands, in every coordinate space the client needs.

    ``lat``/``lon`` is what the map renders from (GeoJSON, RFC 7946 order); the
    projected pair is kept so the client does not have to invert the
    projection to place a label or measure a distance.
    """

    q: int
    r: int
    world_x: float
    world_y: float
    lat: float
    lon: float
    is_new: bool
    """False when this was already yours and the call was a no-op retry. Lets
    the client tell "I just expanded my territory" from "that was already
    mine" without inferring it from the mana balance."""


class ClaimResponse(BaseModel):
    """Result of a claim attempt.

    The full mana state is returned, not just the new balance, for the same
    reason ``ManaSyncResponse`` does: the client replaces its local model with
    server truth rather than tracking a delta and hoping.
    """

    tile: ClaimedTile
    mana_spent: int
    """Zero for a no-op retry of a tile already owned by the caller."""
    mana: int
    mana_max: int
    owned_tile_count: int
    """Total after this claim, so the profile and the map legend update
    without a second round trip."""
    claim_cost: int = TILE_CLAIM_COST
    """Served rather than hardcoded in the client, so the cost the player is
    shown cannot drift from the cost the server charges."""
