"""Single source of truth for every tunable number in StepRealm.

Gameplay numbers live here and *only* here. Secrets and other
environment-specific values live in ``app.core.config``. If a number needs
to change it changes in one of those two files -- never inline in a service,
a route handler, or a migration.

Rationale for keeping this a plain module rather than database rows: the
spec's mana values are decided, documented, and identical in every
environment. Making them configurable-per-deployment would invite the exact
class of bug where a staging database and a production database disagree
about what a tile costs.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Mana economy
# ---------------------------------------------------------------------------
# The values below are reproduced verbatim from the design spec. They are the
# documented, decided MVP numbers. Do not tune them here without changing the
# spec first.

MANA_MAX: Final[int] = 200
"""Hard cap on stored mana. Regeneration that would push a player past this
cap is *discarded*, not banked. Consequence: a player who stays offline for a
week accrues nothing, so mana can never be accumulated by idling."""

MANA_REGEN_AMOUNT: Final[int] = 5
MANA_REGEN_PERIOD_MINUTES: Final[int] = 10
"""Passive regeneration of 5 mana per 10 minutes.

Computed lazily from ``users.last_mana_regen_at`` on every read and write of
a user row, deliberately *not* via a background scheduler. Lazy computation
is correct regardless of how long the service was down, needs no cron, and
has no failure mode where a dead scheduler silently stops mana regen."""

STEPS_PER_MANA_BLOCK: Final[int] = 1000
MANA_PER_STEP_BLOCK: Final[int] = 20
"""Walking reward: each complete block of 1000 steps grants 20 mana.

Blocks are *not* rounded away. A player who walks 1500 steps has a 1.5 block
balance, and settling a sync credits floor(balance) * 20 while carrying the
remainder forward. Discarding the fraction would silently destroy up to 19
mana per sync for anyone who does not walk in exact 1000-step multiples,
which is almost everybody.

The client submits raw step counts and is untrusted (see the anti-cheat note
in app/services/mana_service.py), so step deltas must be validated for
monotonicity server-side before being credited."""

DAILY_WALKING_MANA_CAP: Final[int] = 200
"""Maximum mana obtainable from walking within one day.

Tracked by ``users.daily_mana_earned``, reset at ``DAILY_RESET_UTC_HOUR`` UTC
via ``users.daily_mana_reset_at``. Note this caps *walking* mana only --
passive regeneration is not subject to it, though it is subject to
``MANA_MAX``.

Blocks are indivisible: if the cap leaves 15 mana of headroom, a whole
20-mana block does not fit and nothing is granted. The leftover headroom is
forfeited rather than carried, so a player cannot accumulate a buffer across
days and then spend it in one go."""

MAX_STEPS_PER_SYNC: Final[int] = 250_000
"""Sanity ceiling on the step delta a single ``POST /mana/sync`` may submit,
roughly 190 km.

This is input validation, not anti-cheat. It exists to catch a malfunctioning
or malicious client sending an absurd value, and to fail loudly rather than
quietly. It is explicitly *not* a defence against cheating: the spec scopes
production anti-cheat out of the MVP, and a client that walks 10,000 steps can
still simply claim it. The real economic bound is
``DAILY_WALKING_MANA_CAP``, which caps how much any step count can ever be
worth."""


TILE_CLAIM_COST: Final[int] = 20

INITIAL_MANA: Final[int] = 20
"""NOT SPECIFIED BY THE DESIGN DOC -- flagged for product review.

Chosen so a brand new player can claim the hex they are standing on during
their first session, instead of being locked out of the core loop for the 40
minutes it would take passive regen to cover one tile. Set to 0 for a
stricter economy that forces the first walk to earn the first claim."""

# ---------------------------------------------------------------------------
# Hex grid
# ---------------------------------------------------------------------------

HEX_SIZE_METERS: Final[float] = 40.0
"""Circumradius ``S`` of a hex, in **Web Mercator plane metres**.

Neighbour centre-to-centre spacing is ``sqrt(3) * S`` ~= 69 m, so at S=40 one
tile of territory corresponds to roughly a one-minute walk. Tuned so the
viewport below spans a real neighbourhood rather than a single block.

These are metres in the *projected* plane, not on the ground, and that is a
deliberate choice. Web Mercator stretches lengths by ``1 / cos(latitude)``, so
this constant is only ground-accurate at the equator: at 40 degrees north a
"S=40" hex actually spans about 53 m of real ground, and at 60 degrees north
about 35 m (see ``geo.ground_distance_scale``).

The alternative -- dividing by ``cos(latitude)`` so the *ground* span is a true
40 m everywhere -- was rejected because the client renders the grid in
projected space on a Web Mercator basemap. Compensating would make the on-screen
hexes non-regular and visibly stretched, which reads as a rendering bug. Keeping
the projection honest keeps the overlay geometrically correct, which matters far
more for a game whose grid is drawn over a real map. If a true ground-metre
tile size is ever needed, it must be compensated in the *inverse* direction
only, in ``geo.lonlat_to_axial``.

NOTE: orientation is implied by this constant plus ``app.services.geo`` and is
not stored per row. Pointy-top is used throughout. Changing orientation would
reinterpret every ``(q, r)`` already in ``hex_tiles`` and require a full
re-projection."""

HEX_NEIGHBOR_OFFSETS: Final[tuple[tuple[int, int], ...]] = (
    (1, 0),
    (1, -1),
    (0, -1),
    (-1, 0),
    (-1, 1),
    (0, 1),
)
"""The six axial neighbour offsets, clockwise from (+1, 0).

Every (q, r) plus each of these is one of the six adjacent tiles. Adjacency
for territory claims is checked against this tuple only -- never via a
distance heuristic."""

WORLD_VIEWPORT_RINGS: Final[int] = 16
"""A hexagon of radius R contains exactly ``3R(R+1)+1`` tiles, so R=16 is
817 tiles. This is the "roughly 800 tiles" figure from the spec, and it is a
*viewport* size, not a world size: the client renders a procedural hex grid
over a real-world map (Pokemon-Go style), so the plane is unbounded and tiles
are materialised lazily on first claim.

The spec's phrasing -- "generate a region of roughly 800 tiles around a
centre coordinate" combined with "GET /world ... not the whole 800-tile world
in one response" -- is internally inconsistent under a real-world map, since
800 tiles at S=40 covers ~1.1 km and no player standing further out could ever
claim anything. Reinterpreting 800 as the page size keeps the number and
makes the game playable everywhere. FLAG: if a bounded arena is actually
wanted, set ``config.world_max_radius_from_origin`` rather than seeding."""

WORLD_VIEWPORT_MAX_TILES: Final[int] = 1200
"""Hard server-side ceiling on tiles returned by ``GET /world``, applied
independently of ``WORLD_VIEWPORT_RINGS`` so a hostile client cannot request
a continent by asking for a large radius."""

WORLD_VIEWPORT_MAX_RINGS: Final[int] = 19
"""Largest ``rings`` the client may ask for, derived from the tile budget
above: a hexagon of radius 19 holds ``3*19*20+1 = 1141`` tiles and radius 20
would hold 1261, over budget. Exposed as a constant so the OpenAPI schema
publishes the limit, and re-derived in a test so the two cannot drift apart.
"""

WORLD_MAX_LATITUDE_MERCATOR: Final[float] = 85.05112878
"""Latitude beyond which the Web Mercator projection is unusable.

At the poles the projection diverges (``tan(pi/4 + pi/2)`` is infinite), so
``geo.lonlat_to_world`` clamps to this bound -- the conventional one, where the
projection is exactly square and the whole world fits a 20037508 m square. The
API rejects latitudes beyond +/-90 outright and clamps those within the dead
zone, so a GPS fix near the pole yields an imprecise position rather than a
500."""

# The optional leash on the procedural plane lives in ``app.core.config`` as
# ``world_max_radius_from_origin``, not here. Unlike every other number in this
# module it is not a property of the game but of a deployment: an operator
# choosing between a real-world map (unbounded) and a fenced arena (bounded)
# picks different values for the same code, and the honest default -- unbounded
# -- is the one that makes the game playable outside a single city. Keeping it
# in config means closing the first-claim anti-cheat hole is an environment
# variable rather than a code change.


WEB_MERCATOR_RADIUS_METERS: Final[float] = 6378137.0
"""``R`` for the spherical Web Mercator projection used by the location ->
axial pipeline (the same constant MapLibre/Mapbox use)."""

# ---------------------------------------------------------------------------
# Response precision
# ---------------------------------------------------------------------------

COORDINATE_PRECISION: Final[int] = 3
"""Decimal places for *projected* coordinates sent to the client.

A millimetre, which is roughly eight orders of magnitude finer than a hex is
wide, and it stops every coordinate in the response carrying 17 significant
digits of float noise."""

COORDINATE_PRECISION_DEGREES: Final[int] = 7
"""Decimal places for *degree* coordinates sent to the client.

A completely different quantity from :data:`COORDINATE_PRECISION`, which is why
it is a separate constant. One hundredth of a millidegree -- the finest
resolution every slippy-tile renderer uses -- is about 1.1 cm, comfortably
below the 40 m hex.

Reusing the projected constant here would be a silent disaster: three decimal
places of *latitude* is 111 m, wider than two hexes. The client would draw
every hex displaced from its true position, with visible gaps and overlaps
between neighbours, and no range check would catch it, because a 111 m error
still lands well inside +/-90. See ``test_world.test_tile_lon_lat_agrees_with_
the_projected_centre``."""

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

MIN_PASSWORD_LENGTH: Final[int] = 8

# ---------------------------------------------------------------------------
# Display name
# ---------------------------------------------------------------------------

DISPLAY_NAME_MAX_LENGTH: Final[int] = 24
DISPLAY_NAME_MIN_LENGTH: Final[int] = 3

DISPLAY_NAME_MAX_ATTEMPTS: Final[int] = 8
"""How many candidate display names a signup will try before giving up.

The first attempt is the name derived from the email; each retry appends a
random suffix, so the candidates are effectively independent and eight tries
make a collision effectively impossible (36**4 suffixes per attempt). The
bound exists so a pathological case cannot spin: the loop is bounded even if
the uniqueness check is broken.
"""

MAX_PASSWORD_BYTES: Final[int] = 72
"""bcrypt's hard input limit, counted in *bytes* not characters.

A naive ``max_length=72`` on the Pydantic field is wrong: 30 emoji are 30
characters but 120 UTF-8 bytes, so it would let through a password that
bcrypt silently truncates -- meaning the last bytes never contribute to the
hash and two different passwords could authenticate each other. The schema
therefore validates the encoded byte length explicitly.

The alternative is the OWASP "bcrypt-sha256" pattern (SHA-256 the password,
base64 it, then bcrypt), which removes the limit entirely. Not needed for
this MVP: rejecting an over-long password up front is simpler and honest,
and 72 bytes is far beyond any password a human or a password manager
produces."""

# ---------------------------------------------------------------------------
# Daily reset
# ---------------------------------------------------------------------------

DAILY_RESET_UTC_HOUR: Final[int] = 0
"""Hour of the UTC day at which ``daily_mana_earned`` returns to zero.

UTC midnight rather than rolling 24h, so the boundary is identical for every
player and needs no per-user offset. Consequence: a player in UTC+13 sees
their daily cap reset mid-afternoon local time. FLAG: if a local-midnight
reset is wanted later, add a timezone column to ``users`` and compute the
next boundary per user -- the reset logic already takes a single timestamp,
so this is a localised change.
"""

# ---------------------------------------------------------------------------
# Rate limiting (phase 6)
# ---------------------------------------------------------------------------

CLAIM_LOCATION_RATE_LIMIT_PER_MINUTE: Final[int] = 10
"""Per-user ceiling on ``POST /territory/claim-by-location``. Claims cost mana
so they are not free to spam, but a location claim is the cheapest way to
probe the world, hence the explicit limit."""

AUTH_LOGIN_RATE_LIMIT_PER_MINUTE: Final[int] = 10
AUTH_SIGNUP_RATE_LIMIT_PER_MINUTE: Final[int] = 5
"""Per-address ceilings on the unauthenticated write endpoints.

Counted per client address rather than per submitted email, deliberately. A
per-email login limit is the more obvious choice and it is a denial-of-service
weapon: anyone who knows a victim's address can lock that account out for the
length of every window by failing to log in ten times, and because the victim
cannot see who did it they can only wait. Per-address bounding puts the blast
radius of the attack on the attacker, and signup is limited more tightly than
login because creating accounts is what actually costs the service something
(email-shaped display names, rows, a token each)."""

RATE_LIMIT_WINDOW_SECONDS: Final[int] = 60
"""Length of a rate-limit window.

A minute because that is the granularity a player reasons about ("I get ten
claims a minute"), so a 429 is explainable to a human without a stopwatch."""


# ---------------------------------------------------------------------------
# Step anti-cheat (phase 6)
# ---------------------------------------------------------------------------

MAX_STEPS_PER_SECOND: Final[float] = 5.0
"""Walking speed ceiling used to sanity-check a step delta against elapsed time.

This is the first thing in the codebase that treats a step count as *implausible*
rather than merely *large*, and it exists because ``MAX_STEPS_PER_SYNC`` does not
help: 250,000 steps is a legitimate single sync for a player who walked all
afternoon offline, so the same number is fine once an hour and absurd ten times
a minute. Only the ratio of steps to elapsed time separates those.

Five steps per second is about 18,000 steps an hour, or a 16 km walk -- far
beyond a normal session, chosen so that a real player never brushes the limit
and a script submitting a fixed large number every few seconds does. A step is
counted by the phone, so real-world cadence is roughly 1.5-2.5 steps per second;
the cap is set at more than double the top of that range on purpose. It is a
speed limit, not a step budget: mana is bounded by ``DAILY_WALKING_MANA_CAP``
regardless, and this exists to make a blatant cheat visibly fail rather than to
be the thing that makes cheating impossible.
"""

