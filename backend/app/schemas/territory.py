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

from app.core.constants import MAX_WALK_REPORT_METERS, TILE_CLAIM_COST


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
    reason ``WalkReportResponse`` does: the client replaces its local model
    with server truth rather than tracking a delta and hoping.
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


class WalkReportRequest(BaseModel):
    """Report metres walked since the previous report.

    ``lat``/``lon`` is the coordinate the distance window *started* at, not a
    live position. The server projects it to a hex with the same
    ``lonlat_to_axial`` every claim uses and credits the metres there, so the
    client never has to reimplement cube rounding and cannot disagree with the
    backend about which tile a stretch of walking happened in.
    """

    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    meters: float = Field(
        gt=0,
        le=MAX_WALK_REPORT_METERS,
        description=(
            "Metres covered since the previous report. Bounded above by "
            "MAX_WALK_REPORT_METERS; larger values are refused server-side."
        ),
    )


class WalkReportResponse(BaseModel):
    """The state of the hex the report landed in, after crediting.

    ``total`` is the server-owned accumulated metres and is what the claim gate
    will actually read, so the client can render progress from it rather than
    from its own arithmetic. ``required`` and ``remaining`` keep the "walk 20 m
    in this hex" number where the server can change it without a client
    release.

    The mana fields carry the *full* balance state, the same way a claim does:
    walking is now the only way to earn mana, so the walk report is the one
    moment the client can safely bring its balance forward -- showing the mana
    earned and where it landed beats the client guessing.
    """

    q: int
    r: int
    total: float
    """Accumulated metres in this hex after this report was credited."""
    required: float
    """The distance needed before this hex can be claimed."""
    remaining: float
    """``max(0, required - total)``, so the UI can say "just X more metres"."""
    mana: int
    mana_max: int
    walking_gained: int
    """Mana earned from this report's metres."""
    passive_gained: int
    """Mana that passive regen accrued since the last settlement."""
    total_mana_gained: int
    """``walking_gained + passive_gained``, the change to the balance."""
    daily_mana_earned: int
    """Walking mana counted against ``DAILY_WALKING_MANA_CAP`` today."""
    daily_mana_remaining: int
    """``max(0, DAILY_WALKING_MANA_CAP - daily_mana_earned)``."""
