/**
 * Runtime checks that a response matches the hand-written type in
 * `src/api/types.ts`.
 *
 * TypeScript types vanish at runtime, so a field renamed on the server is
 * invisible to the compiler and shows up as `undefined` deep inside a map
 * render. These functions are the thing that actually catches that drift, which
 * is the whole reason the types are hand-written rather than generated.
 *
 * Written by hand instead of pulled from a validation library on purpose: the
 * point is to assert the *documented contract* rather than to re-derive a
 * schema that could itself drift from both sides.
 */

export class ShapeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ShapeError';
  }
}

function requireObject(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new ShapeError(`${label} should be an object, got ${describe(value)}`);
  }
  return value as Record<string, unknown>;
}

function describe(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

function requireField(obj: Record<string, unknown>, key: string, label: string): unknown {
  if (!(key in obj)) {
    throw new ShapeError(`${label} is missing the field "${key}"`);
  }
  return obj[key];
}

function requireNumber(obj: Record<string, unknown>, key: string, label: string): number {
  const value = requireField(obj, key, label);
  if (typeof value !== 'number' || Number.isNaN(value)) {
    throw new ShapeError(`${label}.${key} should be a number, got ${describe(value)}`);
  }
  return value;
}

function requireString(obj: Record<string, unknown>, key: string, label: string): string {
  const value = requireField(obj, key, label);
  if (typeof value !== 'string') {
    throw new ShapeError(`${label}.${key} should be a string, got ${describe(value)}`);
  }
  return value;
}

function requireBoolean(obj: Record<string, unknown>, key: string, label: string): boolean {
  const value = requireField(obj, key, label);
  if (typeof value !== 'boolean') {
    throw new ShapeError(`${label}.${key} should be a boolean, got ${describe(value)}`);
  }
  return value;
}

function requireArray(obj: Record<string, unknown>, key: string, label: string): unknown[] {
  const value = requireField(obj, key, label);
  if (!Array.isArray(value)) {
    throw new ShapeError(`${label}.${key} should be an array, got ${describe(value)}`);
  }
  return value;
}

function requireNullableNumber(
  obj: Record<string, unknown>,
  key: string,
  label: string,
): number | null {
  const value = requireField(obj, key, label);
  if (value === null) {
    return null;
  }
  if (typeof value !== 'number' || Number.isNaN(value)) {
    throw new ShapeError(`${label}.${key} should be a number or null, got ${describe(value)}`);
  }
  return value;
}

export function assertUser(value: unknown, label = 'user'): void {
  const obj = requireObject(value, label);
  requireNumber(obj, 'id', label);
  requireString(obj, 'email', label);
  requireString(obj, 'display_name', label);
  requireNumber(obj, 'mana', label);
  requireNumber(obj, 'mana_max', label);
  requireNumber(obj, 'owned_tile_count', label);
  const college = requireField(obj, 'college', label);
  if (college !== null) {
    const c = requireObject(college, `${label}.college`);
    requireNumber(c, 'id', `${label}.college`);
    requireString(c, 'name', `${label}.college`);
    requireString(c, 'join_code', `${label}.college`);
  }
}

export function assertTokenResponse(value: unknown, label = 'token response'): void {
  const obj = requireObject(value, label);
  requireString(obj, 'access_token', label);
  requireString(obj, 'token_type', label);
  requireNumber(obj, 'expires_in_seconds', label);
  assertUser(obj['user'], `${label}.user`);
}

export function assertWorldResponse(value: unknown, label = 'world'): void {
  const obj = requireObject(value, label);
  const centre = requireObject(requireField(obj, 'centre', label), `${label}.centre`);
  for (const key of ['q', 'r', 'lat', 'lon', 'world_x', 'world_y']) {
    requireNumber(centre, key, `${label}.centre`);
  }
  requireNumber(obj, 'rings', label);
  requireNumber(obj, 'tile_count', label);
  requireNumber(obj, 'owned_count', label);
  requireNumber(obj, 'hex_size_meters', label);
  requireNullableNumber(obj, 'viewer_id', label);
  requireBoolean(obj, 'latitude_was_clamped', label);

  const bounds = requireObject(requireField(obj, 'bounds', label), `${label}.bounds`);
  for (const key of [
    'min_lon', 'max_lon', 'min_lat', 'max_lat',
    'min_world_x', 'max_world_x', 'min_world_y', 'max_world_y',
  ]) {
    requireNumber(bounds, key, `${label}.bounds`);
  }

  const tiles = requireArray(obj, 'tiles', label);
  for (const tile of tiles) {
    const t = requireObject(tile, `${label}.tile`);
    for (const key of ['q', 'r', 'world_x', 'world_y', 'lat', 'lon']) {
      requireNumber(t, key, `${label}.tile`);
    }
    const owner = requireField(t, 'owner', `${label}.tile`);
    if (owner !== null) {
      const o = requireObject(owner, `${label}.tile.owner`);
      requireNumber(o, 'id', `${label}.tile.owner`);
      requireString(o, 'display_name', `${label}.tile.owner`);
    }
  }
}

export function assertClaimResponse(value: unknown, label = 'claim'): void {
  const obj = requireObject(value, label);
  const tile = requireObject(requireField(obj, 'tile', label), `${label}.tile`);
  for (const key of ['q', 'r', 'world_x', 'world_y', 'lat', 'lon']) {
    requireNumber(tile, key, `${label}.tile`);
  }
  requireBoolean(tile, 'is_new', `${label}.tile`);
  requireNumber(obj, 'mana_spent', label);
  requireNumber(obj, 'mana', label);
  requireNumber(obj, 'mana_max', label);
  requireNumber(obj, 'owned_tile_count', label);
  requireNumber(obj, 'claim_cost', label);
}

export function assertManaSyncResponse(value: unknown, label = 'mana sync'): void {
  const obj = requireObject(value, label);
  for (const key of [
    'mana', 'mana_max',
    'passive_mana_gained', 'walking_mana_gained', 'total_mana_gained',
    'steps_credited', 'pending_step_remainder',
    'daily_mana_earned', 'daily_mana_remaining',
    'mana_forfeited_to_daily_cap', 'mana_forfeited_to_mana_max',
  ]) {
    requireNumber(obj, key, label);
  }
  // An ISO-8601 string, and a real one: a mana bar that renders
  // "Invalid Date" because this came back as a number is exactly the kind of
  // silent mismatch these assertions exist to prevent.
  const resetAt = requireString(obj, 'daily_reset_at', label);
  if (Number.isNaN(Date.parse(resetAt))) {
    throw new ShapeError(`${label}.daily_reset_at is not a parseable date: ${resetAt}`);
  }
}
