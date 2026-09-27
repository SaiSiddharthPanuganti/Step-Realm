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
