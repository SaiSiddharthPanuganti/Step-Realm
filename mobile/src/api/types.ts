/**
 * Wire types for the StepRealm API.
 *
 * These mirror the Pydantic schemas in `backend/app/schemas/` one-for-one.
 * They are hand-written rather than generated, and that is a deliberate
 * trade: codegen would need the backend running and would put a build step
 * between an API change and a type error. Hand-written means a backend change
 * that these miss shows up as a test failure in `tests/api.contract.test.ts`,
 * which asserts against the live server -- so the drift is caught either way,
 * and this file stays readable.
 *
 * Field names are snake_case because that is what the server sends. Renaming
 * them to camelCase on the way in would be tidier to read and would mean
 * writing a mapping layer for every response, which is where silent mismatches
 * live. The wire format wins; the UI layer can prettify if it wants to.
 */

// --- auth -------------------------------------------------------------------

export interface CollegeSummary {
  id: number;
  name: string;
  join_code: string;
}

export interface User {
  id: number;
  email: string;
  /** The public label on territory. Safe to show to other players. */
  display_name: string;
  mana: number;
  /** Served by the server so the client's mana bar cannot drift from the cap. */
  mana_max: number;
  college: CollegeSummary | null;
  owned_tile_count: number;
}

export interface TokenResponse {
  access_token: string;
  token_type: 'bearer';
  expires_in_seconds: number;
  /** Included so the client can render a profile without a second round trip. */
  user: User;
}

/** The one, enforced way to pick a player-facing display name. */
export interface DisplayNameUpdate {
  display_name: string;
}

// --- territory --------------------------------------------------------------

export interface ClaimedTile {
  q: number;
  r: number;
  world_x: number;
  world_y: number;
  lat: number;
  lon: number;
  /** False for a no-op retry of a tile the caller already owns. */
  is_new: boolean;
}

export interface ClaimResponse {
  tile: ClaimedTile;
  /** Zero for a no-op retry. */
  mana_spent: number;
  mana: number;
  mana_max: number;
  /** Total after this claim, so the legend updates without a second request. */
  owned_tile_count: number;
  /** Served by the server so the cost shown cannot drift from the cost charged. */
  claim_cost: number;
}

export interface ClaimByLocationRequest {
  lat: number;
  lon: number;
}

export interface WalkReportRequest {
  lat: number;
  lon: number;
  /**
   * Metres walked *since the last successful report in this hex*, never a
   * running total. Same rule as steps: a client that resends an absolute count
   * gets credited for the same walk on every report.
   */
  meters: number;
}

export interface WalkReportResponse {
  /** The hex the server attributed the reported metres to. */
  q: number;
  r: number;
  /** Total metres banked inside that hex, including this report. */
  total: number;
  /** From the server, so the HUD cannot drift from the gate that enforces it. */
  required: number;
  /** 0 when the hex is claimable. Negative only if a hex became claimable elsewhere. */
  remaining: number;
  /** The account balance and cap after this report, if the client ever shows them. */
  mana: number;
  mana_max: number;
  /** Mana this report earned, which is its walking distance in whole metres. */
  walking_gained: number;
  /** Passive regen the report surfaced while refreshing the balance. */
  passive_gained: number;
  /** `walking_gained + passive_gained`; what the player actually gained. */
  total_mana_gained: number;
  /** The player's cumulative earnings so far today, against the daily cap. */
  daily_mana_earned: number;
  /** What is left of the daily cap before it stops paying out. */
  daily_mana_remaining: number;
}

// --- world ------------------------------------------------------------------

export interface TileOwner {
  id: number;
  display_name: string;
}

export interface WorldTile {
  q: number;
  r: number;
  world_x: number;
  world_y: number;
  /**
   * WGS84 centre, and what the map consumes.
   *
   * RFC 7946 fixes GeoJSON coordinate order as [lon, lat]. Reversing it is the
   * single easiest way to get a map that renders somewhere plausible but
   * wrong, so `toGeoJSON` in `src/map/geojson.ts` is the only place that
   * assembles positions, and it is covered by its own tests.
   */
  lat: number;
  lon: number;
  /** null means unclaimed, which is the overwhelmingly common case. */
  owner: TileOwner | null;
  /**
   * Metres this viewer has banked inside this hex, or 0 for an anonymous
   * caller. Per-viewer on purpose, so a map shown to someone else cannot be
   * used to read another player's position history.
   */
  walk_meters: number;
}

export interface WorldCentre {
  q: number;
  r: number;
  lat: number;
  lon: number;
  world_x: number;
  world_y: number;
}

export interface WorldBounds {
  min_lon: number;
  max_lon: number;
  min_lat: number;
  max_lat: number;
  min_world_x: number;
  max_world_x: number;
  min_world_y: number;
  max_world_y: number;
}

export interface WorldResponse {
  centre: WorldCentre;
  rings: number;
  tile_count: number;
  owned_count: number;
  hex_size_meters: number;
  /** The authenticated caller, or null. Compare against `tile.owner.id`. */
  viewer_id: number | null;
  /**
   * True when the backend could not reach OpenStreetMap and fell back to
   * showing everything unclaimed-able. Walkable-only boards arrive after a
   * retry; the map can hint that instead of silently changing underfoot.
   */
  walkability_degraded: boolean;
  /**
   * True when the requested latitude exceeded the Web Mercator limit and the
   * centre was pulled back. Surfaced rather than hidden so the client can say
   * "this is as close as the projection gets" instead of silently lying.
   */
  latitude_was_clamped: boolean;
  bounds: WorldBounds;
  tiles: WorldTile[];
  /**
   * The metres a player must bank inside a hex before claiming it, from the
   * server's settings so the HUD cannot disagree with the gate that enforces
   * it. Zero on servers where walk-distance claiming is switched off.
   */
  claim_required_meters: number;
}

// --- leaderboard ------------------------------------------------------------

/**
 * One ranked player. `rank` is competition-style: players with the same
 * territory count share a rank, and the next distinct score skips the gap
 * (1, 2, 2, 4).
 */
export interface LeaderboardEntry {
  rank: number;
  user_id: number;
  display_name: string;
  mana: number;
  owned_tile_count: number;
}

/**
 * The signed-in caller's own standing, always included so a player who has
 * not cracked the top of the page still sees where they are. `rank` is None
 * while they own nothing ("unranked" rather than "last").
 */
export interface LeaderboardViewer {
  user_id: number;
  display_name: string;
  mana: number;
  owned_tile_count: number;
  rank: number | null;
}

export interface LeaderboardResponse {
  /** Echoes the `limit` the caller asked for. */
  limit: number;
  /**
   * True when the server served the board straight from Postgres because
   * Redis could not help. The ranking is identical either way; the flag only
   * exists so the UI can choose to label it a snapshot.
   */
  degraded: boolean;
  entries: LeaderboardEntry[];
  viewer: LeaderboardViewer | null;
}

// --- errors -----------------------------------------------------------------

/**
 * Every status the API uses, named.
 *
 * The client branches on these, so they are a typed union rather than loose
 * numbers. A 402 in particular is not a typo: it is how "you cannot afford
 * this claim" is reported, and a client that treats it as a generic failure
 * will show the player a network error for a perfectly ordinary empty mana bar.
 */
export type ApiStatus =
  | 400
  | 401
  | 402
  | 409
  | 422
  | 429
  | 500;
