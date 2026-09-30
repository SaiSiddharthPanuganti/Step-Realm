# StepRealm

Walk to earn mana. Spend it to take the ground you are standing on.

StepRealm is a location-based territory game. The world is an unbounded plane of
40-metre hexes drawn over a real map. Walking earns mana; mana buys hexes; hexes
are the score. There is no way to buy mana, skip the walk, or claim ground you
have not physically stood on.

The repository holds two deployables:

| Path      | What it is                     | Stack                                                     |
| --------- | ------------------------------ | --------------------------------------------------------- |
| `backend/`| HTTP API, game rules, economy  | FastAPI, SQLAlchemy 2, Postgres 16, Redis 7, Python 3.14   |
| `mobile/` | Expo app for Android and iOS  | Expo SDK 57, React Native 0.86, React 19, TypeScript       |

376 backend tests and 122 mobile tests, all passing.

---

## Table of contents

- [How the game works](#how-the-game-works)
  - [The core loop](#the-core-loop)
  - [The mana economy](#the-mana-economy)
  - [The hex grid](#the-hex-grid)
  - [Walkability](#walkability)
  - [The leaderboard](#the-leaderboard)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Repository layout](#repository-layout)
- [API reference](#api-reference)
- [Data model](#data-model)
- [How the app measures walking](#how-the-app-measures-walking)
- [Security design](#security-design)
- [Testing](#testing)
- [Architecture notes](#architecture-notes)
- [Known gaps](#known-gaps)

---

## How the game works

### The core loop

1. You sign up. Your account gets a display name derived from your email and a
   balance of **0 mana**.
2. You walk. Real GPS movement is measured on the phone and reported to the
   server, which prices it at **1 mana per metre**.
3. Once you have walked **20 metres inside a hex**, that hex becomes claimable.
4. Claiming costs **20 mana** and takes the hex out of circulation forever.
5. Every later hex must **touch territory you already hold**, so your empire
   grows as a connected blob rather than a scatter of islands.

The numbers are not decorative. They interlock exactly:

```
MANA_PER_METER      = 1.0
TILE_CLAIM_COST     = 20
TILE_CLAIM_MIN_METERS = 20.0
```

Walking the 20 m inside a hex earns exactly the 20 mana that claiming it costs.
**The first walk funds the first tile**, and because `INITIAL_MANA = 0` there is
no free claim that bypasses this. The economy is self-balancing by
construction, not by tuning.

### The mana economy

All gameplay numbers live in `backend/app/core/constants.py` and nowhere else.
Deployment knobs live in `backend/app/core/config.py`. The separation is
deliberate: a staging database and a production database must never disagree
about what a tile costs.

| Constant                    | Value     | Meaning                                        |
| --------------------------- | --------- | ---------------------------------------------- |
| `MANA_MAX`                  | `200`     | Hard cap on stored mana                        |
| `MANA_REGEN_AMOUNT`         | `5`       | Passive regen per period                       |
| `MANA_REGEN_PERIOD_MINUTES` | `10`      | Length of that period                          |
| `MANA_PER_METER`            | `1.0`     | Mana per validated metre walked               |
| `DAILY_WALKING_MANA_CAP`    | `2000`    | Ceiling on mana from walking, per UTC day      |
| `TILE_CLAIM_COST`           | `20`      | Mana to claim one hex                          |
| `INITIAL_MANA`              | `0`       | Starting balance                               |
| `DAILY_RESET_UTC_HOUR`      | `0`       | Midnight UTC, not a rolling 24 h              |

**Passive regeneration is lazy, not scheduled.** There is no cron job. Every read
and every write of a user row settles regen from the `last_mana_regen_at`
watermark. That is correct regardless of how long the service was down, needs no
scheduler, and has no failure mode where a dead worker silently stops regen. The
watermark advances by *whole* periods only, so a player syncing every six
minutes is not permanently four minutes short.

**Overflow is discarded, not banked.** A player sitting at the 200 cap accrues
nothing, and the watermark still advances. This is the anti-hoarding rule: you
cannot bank a week offline and then spend it retroactively.

**Walking is the only faucet.** Passive regen is capped by `MANA_MAX` but is not
subject to the daily cap. Four rules bite, in this order:

1. **Truncation.** `25.9 m` grants 25 mana, `0.9 m` grants 0. At 1:1 the loss is
   at most one mana per report. The fraction is dropped, never banked.
2. **The daily cap bites first, and forfeited distance is dropped rather than
   banked.** Otherwise you could accumulate a buffer today and spend it
   tomorrow.
3. **The daily counter records mana *earned*, not mana *landed*.** This is the
   subtlest rule in the file. If the cap only counted mana that actually landed,
   a player at the 200 cap could earn a further 2000 mana into a full bar and
   then have a full bar the instant they spent 20 on a tile — a straight bypass
   of the daily limit.
4. **`MANA_MAX` is a holding cap, not an earning cap.** Mana earned above 200 is
   absorbed but still counted against the daily cap.

**There are no multipliers.** No streaks, no college bonuses, no defence
discounts. Mana is strictly linear in metres, gated only by the three caps.

**Settlement order** (`settle_mana`) is fixed: roll the daily reset, apply
passive regen, then apply walking. Regen goes first so the walk fills whatever
headroom is left, which matches the intuition that regen accrued over time and
the walk is the thing happening now.

### The hex grid

The grid is **pointy-top axial**, projected through **spherical Web Mercator**
(the same EPSG:3857 projection MapLibre and Mapbox render in, using the same
radius constant). The pipeline is:

```
lat/lon  --(Web Mercator)-->  plane metres
         --(pointy-top)----->  fractional axial
         --(cube rounding)-->  integer axial (q, r)
```

Cube rounding rather than rounding `q` and `r` independently, because axial
coordinates are a redundant 2-D encoding of a 3-D cube point where `x + y + z =
0`. Rounding two axes separately can produce a pair that corresponds to no hex
at all. Rounding all three and repairing the largest error guarantees a real
hex.

`HEX_SIZE_METERS = 40.0` is the circumradius in **projected plane metres**, not
ground metres. This is deliberate and worth understanding:

- Web Mercator stretches lengths by `1 / cos(latitude)`, so a length measured in
  projected plane is `cos(lat)` times its true length on the ground.
- A "40 m" hex therefore spans about 31 m of real ground at 40° north and 20 m
  at 60° north. (`HEX_SIZE_METERS` and `geo.ground_distance_scale` describe this
  the other way round, claiming 53 m and 35 m; the measured values are
  `40 × cos(lat)`, matching `ground_distance_scale`'s own return value.)
- Compensating would make the on-screen hexes visibly non-regular, which reads as
  a rendering bug.
- Keeping the projection honest keeps the overlay geometrically correct, which
  matters more for a grid drawn over a real map.

The practical consequence: a claim needs 20 m of walking inside a hex, and away
from the equator that hex is *smaller* than 20 m of ground. The gate becomes
proportionally easier to satisfy the further north you play.

Hexes materialise lazily. Nothing pre-generates the plane; a row appears the
first time someone tries to claim that `(q, r)`.

Adjacency is six equality tests against a fixed offset tuple, checked against the
tuple only — never a distance heuristic. It stays O(1) no matter how large a
player's holdings grow.

**Orientation is implied by the constant plus `app/services/geo.py` and is not
stored per row.** Changing it would reinterpret every `(q, r)` already in
`hex_tiles` and require a full re-projection.

### Walkability

Hexes only appear where a human could plausibly stand. The server classifies
walkable ground by querying OpenStreetMap through
[Overpass](https://overpass-api.de/api/interpreter), asking for ways tagged
`highway` in this set:

```
footway, path, pedestrian, living_street, residential, service,
unclassified, tertiary, secondary, primary, track, steps
```

plus `leisure=park`. Motorways and trunk roads are **explicitly excluded** — you
cannot legally walk most of those.

Each returned segment is resampled every 20 m (half a hex) and every sample is
snapped to a hex using the same `lonlat_to_axial` the rest of the server uses.
Sampling rather than snapping OSM nodes alone is necessary because node density
is not uniform: a node every 5 m along a winding footpath and one every 300 m
down a straight residential road are both normal, and node-snapping alone would
punch holes in the second case. A 20 m spacing guarantees at least one sample
lands inside every hex a segment crosses.

Results are cached per 0.02° cell (~2 km) for 600 seconds. A claim only fetches
a 7-hex disc rather than the whole viewport.

**When Overpass is unreachable, both callers fail open and say so.**
`GET /world` sets `walkability_degraded: true` and returns the full grid; a claim
logs a warning and proceeds. The walkability check runs *before* the claim
transaction opens, so a network call never happens while a row lock is held.

This is the only module in the codebase that touches the network.

### The leaderboard

Ranked by **territory**, not mana. With a cap of 200 and claims costing 20,
almost every active player sits at the cap, and a board of ties is not a board.
Territory only ties when two players literally hold the same number of hexes.

Ranking lives in a Redis sorted set (`steprealm:leaderboard:tiles`, score =
`owned_tile_count`), but **Redis picks the players and Postgres counts them.**
A read repairs any score that disagrees with the database, then re-derives the
ordering from the authoritative counts — so ranks do not shuffle depending on
which cache answered. A board that ranks on a stale number is a board a player
cannot act on.

Redis is a cache, not a dependency. When it is down the board is recomputed from
Postgres and the response sets `degraded: true`. **The rankings are identical
either way.**

Ties share a rank (competition ranking: 1, 2, 2, 4). A player who owns no
territory gets `rank: null` — "unranked" is a more honest position than "last".

---

## Quick start

### Prerequisites

- Docker Desktop (Postgres 16 + Redis 7)
- Python 3.14
- Node.js and npm
- Android SDK with `adb` on `PATH`, or an iOS simulator

### One command (Windows)

```powershell
./start.ps1
```

This starts Docker and the two datastores, launches the API on `:8000` with
reload, starts `adb`, and — the part that is easy to forget — runs
`adb reverse tcp:8000 tcp:8000` and `adb reverse tcp:8081 tcp:8081`. On Android,
`localhost` means the phone itself, so without those tunnels the app cannot
reach your machine.

```powershell
./start.ps1 -Stop   # tear down
```

Then in a second terminal:

```bash
cd mobile
npm install
npx expo run:android
```

### Manual setup

**Datastores:**

```bash
docker compose -f backend/docker-compose.yml up -d
docker exec -it steprealm-postgres createdb -U postgres steprealm_test
```

The test database is separate so truncation between test runs can never touch
your dev data.

**Backend:**

```bash
cd backend
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt -r requirements-dev.txt
copy .env.example .env          # then fill in JWT_SECRET
.\.venv\Scripts\alembic upgrade head
.\.venv\Scripts\python -m uvicorn app.main:app --reload --reload-dir app --reload-dir alembic
```

The two `--reload-dir` flags are not optional. Left alone, `--reload` watches all
of `backend/` including `.venv`, and startup takes forever.

**Mobile:**

```bash
cd mobile
npm install
npx expo prebuild --platform android
npx expo run:android
```

### This is not an Expo Go app

`react-native-maps`, `expo-secure-store`, `expo-location`, and
`react-native-reanimated` all carry native code, and `app.config.js` is only read
at prebuild time. Expo Go cannot run this project. Use `npx expo run:android`
or an EAS development build.

There is no `eas.json` in the repo yet, so EAS is documented policy rather than
configured infrastructure.

### Google Maps key

The key must reach the generated Android manifest as
`com.google.android.geo.API_KEY` meta-data. Putting it in `app.json` would
commit it, so `app.config.js` reads it from `GOOGLE_MAPS_API_KEY` or from
`mobile/google-maps-api-key` (git-ignored) and injects it at prebuild time.

With no key, the `react-native-maps` plugin is omitted entirely, so prebuild
still succeeds — the map just cannot authenticate.

There is currently **no iOS equivalent**, so the iOS build has no Google Maps
key wiring.

---

## Configuration

`backend/.env` holds a real `JWT_SECRET`. It is git-ignored and never committed.

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### Backend variables

| Variable                                | Default                     | Purpose                                                        |
| --------------------------------------- | --------------------------- | -------------------------------------------------------------- |
| `ENVIRONMENT`                           | `development`               | Outside dev, the placeholder JWT secret is rejected at startup   |
| `DEBUG`                                 | `true`                      | FastAPI debug flag                                              |
| `LOG_LEVEL`                             | `INFO`                      | Root log level                                                  |
| `DATABASE_URL`                          | local Postgres              | Sync psycopg DSN; also read by Alembic                         |
| `REDIS_URL`                             | local Redis                 | Leaderboard cache. Unset is supported → `degraded: true`        |
| `JWT_SECRET`                            | **required, no default**    | ≥ 32 chars; the `change-me-in-production` prefix is rejected   |
| `JWT_ALGORITHM`                         | `HS256`                     | Passed as an **allowlist** to `jwt.decode`, never read from the token header |
| `ACCESS_TOKEN_EXPIRE_MINUTES`           | `10080` (7 days)            | Token lifetime. There is no refresh token                      |
| `WORLD_MAX_RADIUS_FROM_ORIGIN`          | unset = unbounded plane     | Optional leash; bounds both claims and the map view             |
| `WORLD_ORIGIN_LAT` / `WORLD_ORIGIN_LON` | `0.0`                       | Only consulted when the leash is set                            |
| `AUTH_LOGIN_RATE_LIMIT_PER_MINUTE`      | `10`                        | Per client address                                              |
| `AUTH_SIGNUP_RATE_LIMIT_PER_MINUTE`     | `5`                         | Per client address                                              |
| `CLAIM_LOCATION_RATE_LIMIT_PER_MINUTE`  | `10`                        | Per user                                                        |
| `TRUST_FORWARDED_FOR`                   | `false`                     | Only safe behind a proxy that overwrites `X-Forwarded-For`      |
| `WALKABILITY_ENABLED`                   | `false`                     | Master switch for the Overpass classifier                      |
| `WALKABILITY_OVERPASS_URL`              | Overpass                    | Overpass endpoint                                               |
| `WALKABILITY_TIMEOUT_SECONDS`           | `10.0`                      | Abandon point; the fetch is abandoned, not cancelled            |
| `WALKABILITY_CACHE_CELL_DEGREES`        | `0.02`                      | ~2 km cache cell                                                |
| `WALKABILITY_CACHE_TTL_SECONDS`         | `600.0`                     | Cache lifetime                                                  |

**Mobile variables**

| Variable                | Default                | Purpose                                |
| ----------------------- | ---------------------- | -------------------------------------- |
| `GOOGLE_MAPS_API_KEY`   | unset                  | Android Google Maps SDK key            |
| `EXPO_PUBLIC_API_URL`   | resolved automatically | Override the API base URL              |

When `EXPO_PUBLIC_API_URL` is unset the app derives one: the host that Metro is
served on, with the Android emulator mapped to `10.0.2.2:8000` (inside the
emulator, `localhost` is the emulated device, not your machine).

### Credentials that never reach Git

- `.env` — holds the real `JWT_SECRET`
- `mobile/google-maps-api-key` — the Google Maps key
- `*.a.conf` / `*.i.conf` and `*.a.olf` / `*.i.olf` — Mappls tile credentials,
  bound to the app's package name and signing certificate. Anyone holding them
  can serve map tiles as this app, and Mappls bills the account.
- `.mappls/` — where the config plugin reads them from
- `/mobile/android/` and `/mobile/ios/` — generated by prebuild, and ignored
  outright rather than listing build subdirectories, because the Mappls plugin
  copies credentials into `android/app/`

> **A `.gitignore` note worth keeping.** The Mappls fallback rule is anchored
> with a leading slash (`/app*/`). Written as `app*/` it matched every directory
> beginning with `app` at any depth — including `backend/app/` — and since
> `.gitignore` does not apply to already-tracked files, the effect was invisible:
> existing modules kept committing while every *new* module under `backend/app/`
> was silently dropped. Seven source files were in that trap. If you add ignore
> rules for a path segment, anchor them.

---

## Repository layout

```
.
├── start.ps1                 One-command local launcher (Windows)
├── backend/
│   ├── app/
│   │   ├── main.py           App factory, middleware, /health
│   │   ├── api/
│   │   │   ├── deps.py       Auth dependencies, rate limiters, client IP
│   │   │   └── v1/           auth, me, world, territory, leaderboard
│   │   ├── core/             config, constants, security, logging
│   │   ├── db/               engine, session, redis
│   │   ├── models/           User, HexTile, WalkProgress, College, RateLimit
│   │   ├── schemas/          Pydantic request/response models
│   │   └── services/         All business logic
│   ├── alembic/versions/     0001 → 0006
│   ├── tests/                279 tests
│   └── docker-compose.yml    Postgres 16 + Redis 7
└── mobile/
    ├── app.json              Static config (no secrets)
    ├── app.config.js         Layers in the Google Maps key at prebuild
    ├── plugins/
    │   └── withNdkStlLink.js NDK 27 c++_shared workaround
    └── src/
        ├── api/              HTTP client, endpoints, wire types, errors
        ├── app/              Expo Router screens
        ├── auth/             Session store, SecureStore token
        ├── location/         GPS watch
        ├── map/              Map rendering, hex → GeoJSON
        ├── state/            TanStack Query hooks
        ├── ui/               Theme
        └── walk/             The walking measurement engine
```

### Notable routes in detail

**`GET /world`** is public. A token is optional; a malformed or expired token is
treated as anonymous rather than rejected, because a stale token must not block
the map from drawing. Viewport is `rings` (default 16 → 817 tiles, hard-capped at
19) around a centre.

**`POST /territory/claim-by-location`** takes raw coordinates and lets the
*server* project them, using the same function every other path uses. Latitude
beyond the Mercator limit is clamped, not rejected.

**`POST /territory/walk`** is where mana actually gets minted. The client reports
a distance; the server prices it. See
[How the app measures walking](#how-the-app-measures-walking).

---

## API reference

All routes mount **unprefixed** — there is no `/api/v1` prefix. The
`app/api/v1/` directory is a package name, not a URL prefix.

Interactive docs at `/docs` (Swagger) and `/redoc` when the API is running.

### Auth

| Method | Path               | Auth | Notes                                        |
| ------ | ------------------ | ---- | -------------------------------------------- |
| `POST` | `/auth/signup`     | —    | Returns a token and the user                 |
| `POST` | `/auth/login`      | —    | `401` for both bad email and bad password    |

### Me

| Method | Path                | Auth | Notes                                 |
| ------ | ------------------- | ---- | ------------------------------------- |
| `GET`  | `/me`              | ✔    | Settles mana, so the balance is fresh |
| `PATCH`| `/me/display-name`  | ✔    | 3–24 chars, `[A-Za-z0-9_]`            |

### World

| Method | Path     | Auth | Notes                                       |
| ------ | -------- | ---- | ------------------------------------------- |
| `GET`  | `/world` | —    | Public. `?lat=&lon=&rings=`                 |

### Territory

| Method | Path                         | Auth | Rate limited |
| ------ | ---------------------------- | ---- | ------------ |
| `POST` | `/territory/claim`           | ✔    | —            |
| `POST` | `/territory/claim-by-location` | ✔  | ✔ per user   |
| `POST` | `/territory/walk`            | ✔    | —            |

`claim` takes axial `(q, r)` straight from `GET /world`; `claim-by-location`
takes `lat`/`lon`. Both are idempotent: re-claiming your own hex returns `200`
with `is_new: false` and `mana_spent: 0`.

### Leaderboard

| Method | Path           | Auth | Notes                                    |
| ------ | -------------- | ---- | ---------------------------------------- |
| `GET`  | `/leaderboard` | ✔    | `?limit=` (default 50, max 100)          |

### Ops

| Method | Path          | Notes                                          |
| ------ | ------------- | ---------------------------------------------- |
| `GET`  | `/health`     | `200` if the DB is reachable, else `503`       |
| `GET`  | `/health/live`| Always `200`, touches no dependency            |

### Error codes are chosen to be branchable

| Status | Meaning                                                       |
| ------ | ------------------------------------------------------------- |
| `401`  | Invalid or expired token                                      |
| `402`  | Not enough mana. Detail: `claim costs 20 mana, you have N`     |
| `409`  | Hex already taken, or display name taken                      |
| `422`  | Domain rule: not adjacent, not walkable, not enough walking   |
| `429`  | Rate limited, with `Retry-After`                              |

`402 Payment Required` is used specifically because it is how "you cannot afford
this claim" is reported. A client that treats it as a generic failure will show
the player a network error for a perfectly ordinary empty mana bar.

---

## Data model

### `users`

| Column                  | Type          | Notes                                        |
| ----------------------- | ------------- | -------------------------------------------- |
| `id`                    | `BigInteger`  | PK                                            |
| `email`                 | `String(320)` | Unique on `lower(email)`                     |
| `password_hash`         | `String(255)` | bcrypt digest only                           |
| `display_name`          | `String(24)`  | Unique on `lower(display_name)`              |
| `mana`                  | `Integer`     | Current balance                               |
| `last_mana_regen_at`    | `timestamptz` | Lazy-regen watermark                         |
| `daily_mana_earned`     | `Integer`     | Walking mana today, against the daily cap    |
| `daily_mana_reset_at`   | `timestamptz` | Next UTC reset                               |
| `created_at`            | `timestamptz` | Plausibility baseline for a first report     |
| `last_walk_report_at`   | `timestamptz` | Nullable; plausibility baseline thereafter   |
| `college_id`            | `BigInteger`  | FK, `ON DELETE SET NULL`, nullable           |

### `hex_tiles`

`(q, r)` unique — **this is the core invariant.** `owner_id` is nullable because
a row is inserted `NULL` and assigned before commit; a committed row is always
owned. `defense_level` exists but nothing writes it; there is no PvP in this MVP.

### `walk_progress`

`(user_id, q, r)` unique. `meters` is an **accumulated delta updated in place**,
not an event log. The row is **deleted** when the hex is claimed — progress is
spent, not banked.

### `colleges`

Modelled but not yet written to. The college router is a dangling comment in
`v1/router.py`. `total_tile_count` is a denormalised sum incremented in the same
transaction as a claim, and needs a reconciliation query before account deletion
ships.

### `rate_limit_counters`

Composite PK `(subject, scope)`. One row per subject, not per window, so the
table stays proportional to player count rather than to wall-clock time. Stored
in Postgres rather than Redis because a limiter *rejects* requests and must not
live on a dependency that is contractually allowed to vanish.

### Migrations

```
None → 0001 initial_schema
     → 0002 pending_step_remainder   (step economy, since removed)
     → 0003 display_name
     → 0004 rate_limit_counters
     → 0005 step_sync_baseline       (added created_at; still in use)
     → 0006 walk_progress             (head)
```

`alembic.ini` deliberately omits `sqlalchemy.url` — `alembic/env.py` reads
`DATABASE_URL` from the same settings object the service uses, so migrations and
the service cannot disagree.

---

## How the app measures walking

This is the part of the system where a naive implementation quietly ruins the
game, so it is worth reading `mobile/src/walk/accumulate.ts` before changing it.

### Distance is speed × time, not the gap between fixes

A fix is movement only when the chip's own speed measurement says so. GPS
position is accurate to metres, not centimetres, so the distance between two
consecutive fixes is mostly error: a still phone drifts 3–8 m while a walking
phone's true 1 m/s step is invisible under 10–20 m of noise. The provider's speed
is doppler-smoothed and reliable to a fraction of a metre per second.

So **distance is `speed × dt` integrated over the measured steps**, not haversine
over consecutive fixes. Haversine is used only by the no-speed fallback and by
the drift filter.

### Constants

| Constant                              | Value | Meaning                                        |
| ------------------------------------- | ----- | ---------------------------------------------- |
| `WALK_SPEED_FLOOR_METERS_PER_SECOND`  | `1.0` | Below this there is no walking                 |
| `MAX_TRACK_SPEED_METERS_PER_SECOND`   | `4.0` | Matches the server's limit exactly             |
| `MAX_PACE_STEP_SECONDS`               | `3`   | Anti-glitch clamp on a single fix gap          |
| `SPEED_DECISION_FIXES`                | `6`   | Fixes without speed proving the chip cannot report one |
| `MIN_TRACK_DELTA_METERS`              | `2`   | Jitter threshold, fallback mode only           |
| `DRIFT_ESCAPE_METERS`                 | `4`   | When a still fix counts as an escape, not jitter|
| `STILL_CLUSTER_METERS`                | `3`   | Radius of one "still spell"                    |
| `STILL_SETTLE_FIXES`                  | `3`   | Consecutive still fixes that prove a stop      |
| `FALLBACK_MAX_TURN_DEG`               | `100` | Fallback run breaks if it doubles back         |
| `NO_SPEED_FALLBACK_HOPS`              | `2`   | Same-direction hops before metres are earned   |
| `REPORT_FLOOR_METERS`                 | `0.5` | Below this, not worth a network request        |
| `FLUSH_INTERVAL_MS`                   | `5000`| How often pending buckets flush                |

The floor is `1.0` rather than the "already walking" `1.4` deliberately: cheap
doppler speed is smoothed, so a standing phone sits comfortably under half a
metre per second while the same chips throw momentary 0.8–0.9 m/s readings at
idle. `1.0` clears both without making a gentle stroll impossible.

### Two modes, decided once per session

The tracker locks into `speed` or `fallback` on the first fix that reports a
speed, or after 6 fixes. Decided once because a provider that measures speed
reports it every fix and a provider that cannot never will — **flipping between
modes would bank two versions of the same walk**.

In **speed mode** each fix must fall in the closed band `[1.0, 4.0]` m/s. A
non-walking fix drops the phase anchor so the next moving step starts clean and
banks nothing. Credit is attributed to the hex the player was in at the
**segment's start**, so a metre crossing a border is credited to the hex the
walking began in.

The 3-second pace cap is the anti-radio-glitch clamp: a 1.3 m/s step across a
10 s gap claims `1.3 × 3 = 3.9 m`, not 13 m.

In **fallback mode**, a single long hop never earns — two consecutive
same-direction hops do. A turn greater than 100° **breaks the run and throws away
the banked metres**, because doubling back is the fingerprint of jitter.

### The drift filter is about the marker, not the distance

A still spell banks a cluster anchor and settles the marker exactly once, when
three consecutive fixes prove a stop. The marker then holds rather than creeping.
A fix more than 4 m from the released position is treated as a genuine escape —
the player walked while the chip was silent.

### Server-side plausibility

The client is not trusted. The server measures the wall clock between reports
against a server-written baseline (`last_walk_report_at`, falling back to
`created_at` — never a client timestamp) and refuses anything above **4 m/s**:

```
allowance = max(elapsed, 1.0s) × 4.0     # after the first report
allowance = max(elapsed, 30.0s) × 4.0   # the one-time first-report grace
```

The 1 s floor stops two microsecond-apart reports measuring against a near-zero
window. The 30 s grace handles an account that signs up and walks straight into
a hex. It is granted **exactly once, not as a standing allowance.**

This is a speed limit, not a budget. Nothing stops a client reporting as often
as it likes — only from claiming a distance no walk could have covered. A
rejected report leaves the ledger, the balance, **and the baseline untouched**,
so it cannot shrink the next allowance.

### Ledger and flushing

The client keeps an **in-memory** per-hex ledger of pending metres and flushes
every 5 seconds, one in-flight request at a time, one `POST` per hex. On success
it invalidates the world query and ignores the server's totals, because the world
poll carries the authoritative numbers and a second source of truth is how they
drift.

In-memory is deliberate: **the server is the ledger of record.** A session restart
loses only the metres still pending a flush, never the ones already reported.

Before claiming, the map screen `await`s a flush so the justifying metres land
before the request that depends on them.

### Walking earns mana only on the server

The client never computes mana. The wire field is a **delta, never a running
total** — a client that resends an absolute count would be credited for the same
walk on every report. Mana is earned on the *validated report distance*, not the
per-hex accumulated total, so a walk that crosses a border still yields its
distance while the per-hex ledger stays purely the claim gate.

---

## Security design

### Authentication

OAuth2 password bearer, `HS256`, one shared secret. Tokens carry exactly five
claims — `sub`, `type`, `iat`, `nbf`, `exp` — with `type: "access"` so a future
refresh token cannot be replayed as an access token. `nbf == iat` is set rather
than omitted so there is no clock-skew window to tune.

Verification passes `algorithms=[settings.jwt_algorithm]` as an **allowlist**
rather than trusting the token header. That is the defence against `alg: none`
and HS/RS key confusion.

`GET /world` treats a malformed or expired token as anonymous. Every other route
has three rejection paths — no token, bad token, valid token naming a deleted
user — and returns **one indistinguishable** `401`. Distinguishing "expired" from
"malformed" tells an attacker which tokens to keep.

There is **no refresh token and no revocation**. A token is valid for its full
7 days; the only invalidation path is deleting the user row.

### Passwords

bcrypt at 12 rounds. Password length is validated as **UTF-8 bytes ≤ 72**, not
characters: bcrypt's limit is bytes, and a naive character check would let 30
emoji through a 72-character field and be silently truncated by bcrypt.

Login runs a hash comparison against a lazily-built dummy digest when the email
is unknown, so response latency is not an account-enumeration oracle.

### Concurrency

`SELECT ... FOR UPDATE OF users` with `populate_existing=True`, everywhere mana
is touched. The design rule in `territory_service.py` is *"lock in a fixed order,
and let the database decide the rest"* — user row first, then tile row, always in
that order.

For the tile, a `SELECT ... FOR UPDATE` on a non-existent row locks nothing, so
claiming does an `INSERT ... ON CONFLICT DO NOTHING` first and then locks. A
pre-check cannot make two concurrent claims of the same hex safe no matter how
careful it looks, because both readers see "free" before either writes. Whether a
hex is free is decided by the unique index, not by a query.

### Transaction boundaries

Walk reports fold plausibility validation, the ledger write, mana settlement, and
the baseline advance into **one locked transaction**. The metres that buy a
claim are the metres the server itself measured.

Sync SQLAlchemy with psycopg is deliberate over async: psycopg ships reliable
cp314 wheels while asyncpg does not, and expressing the locking guarantees with
threads against a blocking driver is far more direct than the async equivalent,
where the interleaving points are easy to get wrong and a passing test can still
be a false negative.

### Rate limiting

Postgres, one atomic upsert per request, fixed 60 s window anchored on the first
request in it. Fails **open** with a `logger.error` and a rollback — a limiter
that takes the app down when its own storage hiccups is worse than one that lets
a burst through.

The limit is a callable so the ceiling is read per request rather than frozen at
import time. `limit_per_user` declares `get_current_user` as a sub-dependency, so
auth is solved before the body runs and a 401 returns before any counter is
touched.

### Data minimisation

`GET /world` returns an owner's `display_name` and **never an email**.
`WorldTile.walk_meters` is the viewer's own metres only, so a map shown to
someone else cannot be used to read another player's position history. Logs
redact `password`, `password_hash`, `token`, `secret`, and `authorization`.

`PATCH /me/display-name` rejects characters outside `[A-Za-z0-9_]` with a `422`
rather than silently rewriting, so the app never shows a name the player did not
type.

---

## Testing

### Backend — 376 tests

```bash
cd backend
.\.venv\Scripts\python -m pytest
```

Run against `steprealm_test` (database 15 for Redis), truncating between runs.

| File                          | Tests | Covers                                      |
| ----------------------------- | ----: | ------------------------------------------- |
| `test_geo.py`                 |   108 | Projection, cube rounding, adjacency, discs  |
| `test_display_name.py`        |    43 | Sanitisation, uniqueness, migration backfill |
| `test_auth.py`                |    24 | Signup, login, token claims, enumeration    |
| `test_territory.py`           |    40 | Claim ordering, concurrency, every rejection |
| `test_world.py`               |    40 | Viewport bounds, clamping, ownership         |
| `test_mana.py`                |    33 | Regen, walking reward, every cap             |
| `test_rate_limit.py`          |    31 | Fixed window, fail-open, client IP           |
| `test_walkability.py`         |    13 | Overpass parsing and classification          |
| `test_world_leash.py`         |    13 | The optional radius fence                    |
| `test_leaderboard.py`         |    11 | Ranking, ties, Redis/Postgres parity         |
| `test_walk_progress.py`       |    11 | Ledger accumulation, the 1000 m cap          |
| `test_auth_concurrency.py`    |     5 | Concurrent signup and login                  |
| `test_mana_concurrency.py`    |     4 | Concurrent settlement under row locks        |

`test_geo.py` exists because geometry is the one part of the system that must be
provable by arithmetic rather than by integration.

### Mobile — 122 tests

```bash
cd mobile
npm test                  # both projects
npm run test:unit         # pure functions, no server
npm run test:contract     # against a real server
npm run typecheck
npm run lint
```

Two Vitest projects, because they need different worlds. **Unit** tests cover
URL resolution, error parsing, hex→GeoJSON, and the walking accumulator — no
server needed. **Contract** tests (`globalSetup` boots a real uvicorn on port
8099 against the test database, truncated first) assert the client's hand-written
wire types against the live API.

The contract tests deliberately drive a real server rather than a mock, because
the alternative catches a shape mismatch but not a semantic one. They point the
backend at a **separate Redis database** so they cannot collide with the dev
app's cache.

### The client geometry test

`map.contract.test.ts` exists to prove the TypeScript hex→GeoJSON conversion
agrees with the Python one. Two independent implementations of the same
projection will drift; a test that asks the server where a coordinate lands and
compares it against what the client drew is what stops that.

---

## Architecture notes

### The mobile API layer has no React Native imports

`mobile/src/api/client.ts` is deliberately free of `expo-secure-store` and the
constants module. `TokenStore` is an interface and `resolveBaseUrl` is injected
from outside. This keeps the whole API stack runnable under plain Node, which is
why the contract tests can drive a real server without a simulator or a native
module stub. `runtime.ts` is the only file that imports Expo modules.

### Wire types are hand-written on purpose

`mobile/src/api/types.ts` is not generated. A backend change these types miss
shows up as a contract-test failure, so the drift is caught either way — and the
file stays readable. Every field is `snake_case`, because the wire format wins
and the UI layer can prettify.

### The server's answer replaces the local model wholesale

No hook ever applies a delta to mana or territory. Both change as a result of
things happening elsewhere, so a locally-adjusted balance is wrong the moment any
of those happen — usually with nothing visibly broken. A successful claim patches
exactly three fields and then invalidates; everything else is refetched.

### Refetch intervals are deliberately slow

`GET /world` polls every 20 s. The viewport is 7 to 127 tiles, the player is
walking, and the map does not change meaningfully every few seconds. The
leaderboard has **no** refetch interval at all: the board only moves when
someone claims a hex, which is not something that happens while you are looking
at it.

### Sessions are a three-state machine

`restoring | signed-out | signed-in`, not a boolean. A boolean collapses the
first two and the collapse shows up as a sign-in screen flashing on every cold
start while the stored token is checked — the exact moment a player is most
likely to tap the wrong thing. `_layout.tsx` blocks rendering entirely while
restoring.

Tokens live in SecureStore under `WHEN_UNLOCKED_THIS_DEVICE_ONLY`, namespaced so
they cannot collide with another library's keychain entries.

### The NDK 27 workaround

`mobile/plugins/withNdkStlLink.js` exists because NDK 27.1 — the version Expo SDK
57 defaults to — has a toolchain bug. CMake receives `-DANDROID_STL=c++_shared`
but nothing consumes it, so no target links `libc++_shared.so` and the C++ link
step dies with hundreds of `undefined symbol: std::__ndk1::` errors.

Pinning NDK 26 is not available: React Native 0.86 headers need C++20
`std::format`, which r26's libc++ lacks. No NDK version works unpatched.

The plugin patches every `target_link_libraries(...)` in every native module's
CMake files — including `android/cmake/*.cmake`, where `expo-modules-core`
actually declares its links. Patching only the top-level `CMakeLists.txt` leaves
the entire Expo module tree unlinked. It logs what it patched, and warns loudly
when it patched nothing, because a silent no-op turns into hundreds of linker
errors much later.

### Expo version discipline

`mobile/AGENTS.md` carries a standing rule worth repeating here: **Expo ships
breaking changes every SDK release, and training data about it is unreliable.**
Before touching any Expo, EAS, or React Native API, read the major version in
`package.json` (currently 57), fetch `https://docs.expo.dev/versions/v57.0.0/`,
and use `npx expo install <package>` rather than a bare `npm install`. Run
`npx expo lint` and `npx tsc --noEmit` before declaring anything done.

---

## Known gaps

Honest list of what is incomplete, rather than letting you discover it.

**Backend**

- The `HEX_SIZE_METERS` and `ground_distance_scale` docstrings have the
  Mercator stretch backwards. They state a 40 m hex spans 53 m of ground at 40°
  north; measured through the actual projection it spans 30.6 m (`40 × cos(lat)`,
  which is what `ground_distance_scale` itself returns). The code is right and
  the prose is wrong, but the prose is what a future reader will trust. Fixing
  it would also mean deciding whether `TILE_CLAIM_MIN_METERS = 20` should stay a
  projected or become a ground distance.
- `MAX_STEPS_PER_MINUTE`-style knob is still documented in `.env.example` but no
  longer read. `Settings` has no such field; the walking analogue is
  `MAX_WALK_SPEED_METERS_PER_SECONDS = 4.0`.
- `users.pending_step_remainder` (migration 0002) and `users.last_step_sync_at`
  (migration 0005) are residue of the deleted step economy. They remain in
  already-migrated databases and are still dropped by their downgrades, but no
  longer exist in the models.
- `colleges` is modelled and never written. The router is a dangling comment.
- `hex_tiles.defense_level` is reserved and never written — no PvP in this MVP.
- The map "leash" (`WORLD_MAX_RADIUS_FROM_ORIGIN`) is unset by default, so the
  world is unbounded. It is implemented and tested, not enabled.
- **A player's first tile is unrestricted anywhere on Earth.** This is a known
  anti-cheat hole: a spoofed GPS can seed a territory in a foreign country.
  Every later tile must be adjacent. Worth closing.
- `backend/mutations.py` is a stale mutation-testing harness that still assumes
  the step economy.
- No refresh token and no logout/revocation.
- No production deployment config in the repo. `.env.example` notes that Railway
  injects `DATABASE_URL` and `REDIS_URL` from add-ons.

**Mobile**

- No iOS Google Maps key wiring — Android only.
- `expo-sensors` is a configured dependency with **zero imports**. It and the
  `NSMotionUsageDescription` / `ACTIVITY_RECOGNITION` permission strings are
  leftovers from the pedometer implementation. Either planned work or dead weight.
- No `eas.json`; EAS is documented policy, not configured infrastructure.
- The walk tracker keeps its ledger in memory. A hard app kill between a walk and
  the next flush loses the pending metres.
- `FLUSH_INTERVAL_MS` is exported from `accumulate.ts` but the hook hard-codes
  `5_000` instead of importing it.
- The app hard-codes a Bengaluru fallback centre for the first GPS fix.
- Map rendering targets Android. iOS has not been exercised.

**Repo**

- `backend/mutations.py` predates the walk rewrite.
- `.env.example` and the code disagree on the dead `MAX_STEPS_PER_MINUTE` knob.
- The Mercator docstrings above are wrong in the same direction as each other,
  which suggests they were written from the same (incorrect) reasoning rather
  than measured. Worth a second pass over any other prose-only claims in the
  codebase.