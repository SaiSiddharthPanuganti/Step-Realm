/**
 * Turning a `GET /world` response into something a map can draw.
 *
 * Pure, and free of React Native, so the geometry can be tested in Node. This
 * is the part of the client most able to be subtly and invisibly wrong, so it
 * is the part with the most tests.
 *
 * Why any of this exists at all: the server sends each tile's centre, both as
 * lat/lon and as Web Mercator plane metres, but not its six corners. The client
 * could ask for corners, and did not, because the corners are derivable exactly
 * -- and deriving them here means the projection constants are asserted against
 * the server's own answers in `tests/unit/geojson.test.ts` rather than trusted.
 *
 * The alternative of having the client pick a hex from a tap and convert the
 * point to axial coordinates would duplicate the server's projection. It does
 * not do that: the client sends `(q, r)` back exactly as it received it.
 */

import type { WorldResponse, WorldTile } from '../api/types';

/** EPSG:3857. The same value as `app.core.constants`. */
export const WEB_MERCATOR_RADIUS_METERS = 6378137;

/** Latitude beyond which the projection diverges. */
export const MAX_MERCATOR_LATITUDE = 85.05112878;

const DEG_TO_RAD = Math.PI / 180;
const RAD_TO_DEG = 180 / Math.PI;

/** A `[longitude, latitude]` pair, in the order RFC 7946 requires. */
export type LonLat = [number, number];

export interface LngLatBounds {
  min_lon: number;
  max_lon: number;
  min_lat: number;
  max_lat: number;
}

/**
 * Plane metres to WGS84. The exact inverse of the server's projection.
 *
 * `atan(exp(y / R))` is the inverse of Mercator's `ln(tan(pi/4 + lat/2))`.
 * The `2 * atan(...) - pi/2` rearrangement is the standard form: it is
 * well-behaved across the whole range, including at y = 0 where the direct
 * inverse of the forward formula loses precision.
 *
 * `lon` is left unfolded rather than normalised into [-180, 180). A viewport is
 * at most a few hundred metres across, so a tile can never be near the
 * antimeridian, and folding would risk moving a legitimate corner by 360
 * degrees and splitting a hex across the whole map.
 */
export function worldToLonLat(worldX: number, worldY: number): LonLat {
  const lon = (worldX / WEB_MERCATOR_RADIUS_METERS) * RAD_TO_DEG;
  const lat = (2 * Math.atan(Math.exp(worldY / WEB_MERCATOR_RADIUS_METERS)) - Math.PI / 2) * RAD_TO_DEG;
  return [lon, lat];
}

/** WGS84 to plane metres. The server's projection, for tests and for labels. */
export function lonLatToWorld(lon: number, lat: number): [number, number] {
  const clamped = Math.max(-MAX_MERCATOR_LATITUDE, Math.min(MAX_MERCATOR_LATITUDE, lat));
  const x = (lon * DEG_TO_RAD) * WEB_MERCATOR_RADIUS_METERS;
  const y =
    WEB_MERCATOR_RADIUS_METERS *
    Math.log(Math.tan(Math.PI / 4 + (clamped * DEG_TO_RAD) / 2));
  return [x, y];
}

const SQRT3 = Math.sqrt(3);

/** Axial coordinate of a hex, as the server sends it. */
export interface Axial {
  q: number;
  r: number;
}

/** Axial to plane metres, pointy-top. The exact inverse of the server's. */
export function axialToWorld(q: number, r: number, circumradiusMeters: number): [number, number] {
  const x = circumradiusMeters * (SQRT3 * q + (SQRT3 / 2) * r);
  const y = circumradiusMeters * 1.5 * r;
  return [x, y];
}

/** Axial to WGS84 centre. The server's `axial_to_lonlat`. */
export function axialToLonLat(q: number, r: number, circumradiusMeters: number): LonLat {
  const [x, y] = axialToWorld(q, r, circumradiusMeters);
  return worldToLonLat(x, y);
}

/** Fold a longitude into `[-180, 180)`, matching the server's normalisation. */
function normaliseLongitude(lon: number): number {
  return (((lon + 180) % 360) + 360) % 360 - 180;
}

/**
 * Snap a fractional axial coordinate to the nearest real hex.
 *
 * `floor(x + 0.5)` rather than rounding to nearest on each axis alone: on a
 * cube (x + y + z = 0) the two axes are not independent, and rounding them
 * separately can produce a pair that is not a hex at all. The port of
 * `app.services.geo.cube_round`, including the `floor(+0.5)` convention that
 * JS `Math.round` would quietly break (halves round to even there).
 */
function cubeRound(qFraction: number, rFraction: number): Axial {
  const sFraction = -qFraction - rFraction;
  let q = Math.floor(qFraction + 0.5);
  let r = Math.floor(rFraction + 0.5);
  const s = Math.floor(sFraction + 0.5);
  const qError = Math.abs(q - qFraction);
  const rError = Math.abs(r - rFraction);
  const sError = Math.abs(s - sFraction);
  if (qError > rError && qError > sError) {
    q = -r - s;
  } else if (rError > sError) {
    r = -q - s;
  }
  // Clamp the float artefacts of the repair branch: `-0 - 0` is `-0`, and
  // `-0` compares equal to `0` yet is a distinct value to `Object.is`, which
  // is what a deep-equality assertion uses. A returned hex of "negative zero"
  // trips that for a coordinate the caller cannot distinguish anyway.
  return { q: q === 0 ? 0 : q, r: r === 0 ? 0 : r };
}

/**
 * The full client-facing projection: a GPS fix to the tile containing it.
 *
 * Mirrors `app.services.geo.lonlat_to_axial` exactly, including the
 * longitude normalisation and the Mercator latitude clamp. This is the one
 * piece of projection math the client must reproduce, because walk metres are
 * attributed to the hex the *server* will later project the same coordinate
 * into -- the two implementations agreeing is the whole point of having both.
 */
export function lonlatToAxial(lon: number, lat: number, circumradiusMeters: number): Axial {
  const x = WEB_MERCATOR_RADIUS_METERS * normaliseLongitude(lon) * DEG_TO_RAD;
  const clamped = Math.max(-MAX_MERCATOR_LATITUDE, Math.min(MAX_MERCATOR_LATITUDE, lat));
  const y =
    WEB_MERCATOR_RADIUS_METERS *
    Math.log(Math.tan(Math.PI / 4 + (clamped * DEG_TO_RAD) / 2));
  const qFraction = ((SQRT3 / 3) * x - (1 / 3) * y) / circumradiusMeters;
  const rFraction = ((2 / 3) * y) / circumradiusMeters;
  return cubeRound(qFraction, rFraction);
}

/** Ground distance between two WGS84 points, in metres (great-circle). */
export function haversineMeters(a: LonLat, b: LonLat): number {
  // Mean Earth radius, a compromise constant that keeps a "40 m hex" honest
  // to within the metre or two that GPS accuracy dwarfs anyway.
  const EARTH_RADIUS_METERS = 6_371_008.8;
  const lat1 = a[1] * DEG_TO_RAD;
  const lat2 = b[1] * DEG_TO_RAD;
  const dLat = (b[1] - a[1]) * DEG_TO_RAD;
  const dLon = (b[0] - a[0]) * DEG_TO_RAD;
  const h =
    Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_RADIUS_METERS * Math.asin(Math.min(1, Math.sqrt(h)));
}

/**
 * Initial bearing from one point to the next, degrees clockwise from north.
 *
 * The direction of travel when a fix has no compass heading of its own
 * (cheap GPS chips report a heading only while moving). Same convention as
 * `coords.heading`: 0 = north, 90 = east, and the map's direction cone
 * rotates by exactly this number.
 */
export function bearingDegrees(a: LonLat, b: LonLat): number {
  const lat1 = a[1] * DEG_TO_RAD;
  const lat2 = b[1] * DEG_TO_RAD;
  const dLon = (b[0] - a[0]) * DEG_TO_RAD;
  const y = Math.sin(dLon) * Math.cos(lat2);
  const x = Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon);
  return (Math.atan2(y, x) * RAD_TO_DEG + 360) % 360;
}

/**
 * The six corners of a pointy-top hex, as a closed ring.
 *
 * Pointy-top, matching `app.services.geo`: a vertex points north, so the corners
 * sit at 30 degrees plus 60-degree steps rather than at 0 plus 60. Getting this
 * wrong still produces a gapless tiling -- it is just rotated 30 degrees, which
 * means every polygon no longer lines up with the hex the server considers a
 * neighbour. Nothing would error; the map would just be quietly wrong.
 *
 * The radius is the circumradius, centre to corner, which is what
 * `HEX_SIZE_METERS` means on the server. It is a distance in *plane* metres, so
 * a hex is narrower on the ground away from the equator. That is the
 * projection's own distortion and matching it is the point.
 *
 * The ring is closed: GeoJSON requires the first and last position to be
 * identical, and a ring that is not closed is silently not a polygon.
 */
export function hexRing(tile: WorldTile, circumradiusMeters: number): LonLat[] {
  const ring: LonLat[] = [];
  for (let corner = 0; corner < 6; corner += 1) {
    const angle = (30 + 60 * corner) * DEG_TO_RAD;
    ring.push(
      worldToLonLat(
        tile.world_x + circumradiusMeters * Math.cos(angle),
        tile.world_y + circumradiusMeters * Math.sin(angle),
      ),
    );
  }
  ring.push(ring[0]);
  return ring;
}

/**
 * The closed corner ring of the hex at an arbitrary axial coordinate.
 *
 * What the map draws under the player while they walk: the ring of the hex
 * they are *standing in*, which the world response does not include (that tile
 * may be outside the requested viewport). Computed from the same
 * axial-to-plane math the tiles themselves use, so the highlight cannot object
 * to where an ownership polygon sits.
 */
export function hexRingAt(q: number, r: number, circumradiusMeters: number): LonLat[] {
  const [centreX, centreY] = axialToWorld(q, r, circumradiusMeters);
  const ring: LonLat[] = [];
  for (let corner = 0; corner < 6; corner += 1) {
    const angle = (30 + 60 * corner) * DEG_TO_RAD;
    ring.push(
      worldToLonLat(
        centreX + circumradiusMeters * Math.cos(angle),
        centreY + circumradiusMeters * Math.sin(angle),
      ),
    );
  }
  ring.push(ring[0]);
  return ring;
}

/**
 * The hexes that begin labelled, and every hex after the first needs one of
 * these. Shared between the layer builder and the ownership predicate.
 */
export type TileOwnership = 'own' | 'rival' | 'unclaimed';

/**
 * Classify a tile against the viewer.
 *
 * Compared by id, not by display name, because names are not unique and a
 * rename would otherwise change who owns ground.
 */
export function ownershipOf(tile: WorldTile, viewerId: number | null): TileOwnership {
  if (tile.owner === null) {
    return 'unclaimed';
  }
  if (viewerId !== null && tile.owner.id === viewerId) {
    return 'own';
  }
  return 'rival';
}

/** A GeoJSON Feature, kept loose so no @types/geojson dependency is needed. */
export interface Feature {
  type: 'Feature';
  id: string;
  properties: {
    q: number;
    r: number;
    ownership: TileOwnership;
    ownerId: number | null;
    ownerName: string | null;
  };
  geometry: {
    type: 'Polygon';
    /** Always three rings: the outer boundary, then a hole and a shell. */
    coordinates: LonLat[][];
  };
}

export interface FeatureCollection {
  type: 'FeatureCollection';
  features: Feature[];
}

/**
 * One layer per ownership, which is what lets the map style them independently.
 *
 * A single collection with an `ownership` property would need a data-driven
 * style expression, and Mappls' GeoJSON styling does not offer one. Three
 * collections is a few more draw calls and a great deal less configuration.
 */
export interface OwnershipLayers {
  own: FeatureCollection;
  rival: FeatureCollection;
  unclaimed: FeatureCollection;
}

export function toFeature(tile: WorldTile, viewerId: number | null, circumradius: number): Feature {
  const ownership = ownershipOf(tile, viewerId);
  return {
    type: 'Feature',
    // Stable across refreshes, so a map engine can diff features rather than
    // tearing down and rebuilding the whole source on every poll.
    id: `${tile.q},${tile.r}`,
    properties: {
      q: tile.q,
      r: tile.r,
      ownership,
      ownerId: tile.owner?.id ?? null,
      ownerName: tile.owner?.display_name ?? null,
    },
    geometry: {
      type: 'Polygon',
      coordinates: [hexRing(tile, circumradius)],
    },
  };
}

export function worldToLayers(world: WorldResponse): OwnershipLayers {
  const layers: OwnershipLayers = {
    own: { type: 'FeatureCollection', features: [] },
    rival: { type: 'FeatureCollection', features: [] },
    unclaimed: { type: 'FeatureCollection', features: [] },
  };
  for (const tile of world.tiles) {
    layers[ownershipOf(tile, world.viewer_id)].features.push(
      toFeature(tile, world.viewer_id, world.hex_size_meters),
    );
  }
  return layers;
}

/** The centre of a tile, for placing a label or the player's marker. */
export function tileCentre(tile: WorldTile): LonLat {
  return [tile.lon, tile.lat];
}

/**
 * Grow the server's bounds to include a point.
 *
 * Needed because the player's own position can sit outside the requested
 * viewport -- the server centres on a hex, and the player is somewhere inside
 * one, so the marker can land just past the edge. Without this the map frame
 * clips the one marker the player most wants to see.
 */
export function includePoint(bounds: LngLatBounds, point: LonLat): LngLatBounds {
  return {
    min_lon: Math.min(bounds.min_lon, point[0]),
    max_lon: Math.max(bounds.max_lon, point[0]),
    min_lat: Math.min(bounds.min_lat, point[1]),
    max_lat: Math.max(bounds.max_lat, point[1]),
  };
}
