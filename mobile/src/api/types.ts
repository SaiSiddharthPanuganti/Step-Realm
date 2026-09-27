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

// --- mana -------------------------------------------------------------------

export interface ManaSyncRequest {
  /**
   * Steps walked *since the last successful sync*, never a running total.
   *
   * The distinction is not cosmetic. A client that resends an absolute count
   * gets credited for the same walk on every sync, and the daily mana cap is
   * the only thing standing between that and an economy-breaking client.
   */
  steps_delta: number;
}

export interface ManaSyncResponse {
  mana: number;
  mana_max: number;
  passive_mana_gained: number;
  walking_mana_gained: number;
  total_mana_gained: number;
  steps_credited: number;
  /** Steps banked toward the next whole block. Display only; do not trust locally. */
  pending_step_remainder: number;
  daily_mana_earned: number;
  daily_mana_remaining: number;
  daily_reset_at: string;
  /** Non-zero here is the explanation a player needs for a disappointing walk. */
  mana_forfeited_to_daily_cap: number;
  mana_forfeited_to_mana_max: number;
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
   * True when the requested latitude exceeded the Web Mercator limit and the
   * centre was pulled back. Surfaced rather than hidden so the client can say
   * "this is as close as the projection gets" instead of silently lying.
   */
  latitude_was_clamped: boolean;
  bounds: WorldBounds;
  tiles: WorldTile[];
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
