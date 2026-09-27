import { describe, expect, it } from 'vitest';

import {
  hexRing,
  includePoint,
  lonLatToWorld,
  ownershipOf,
  tileCentre,
  toFeature,
  worldToLayers,
  worldToLonLat,
  type LonLat,
} from '../../src/map/geojson';
import type { WorldTile } from '../../src/api/types';

/** Bengaluru, roughly the middle of the reachable world. */
const CENTRE: LonLat = [77.5946, 12.9716];
const SIZE = 40;

function tile(overrides: Partial<WorldTile> = {}): WorldTile {
  const [x, y] = lonLatToWorld(CENTRE[0], CENTRE[1]);
  return {
    q: 0,
    r: 0,
    world_x: x,
    world_y: y,
    lat: CENTRE[1],
    lon: CENTRE[0],
    owner: null,
    ...overrides,
  };
}

describe('projection', () => {
  it('round-trips plane metres through WGS84', () => {
    const [x, y] = lonLatToWorld(CENTRE[0], CENTRE[1]);
    const [lon, lat] = worldToLonLat(x, y);
    expect(lon).toBeCloseTo(CENTRE[0], 9);
    expect(lat).toBeCloseTo(CENTRE[1], 9);
  });

  it('round-trips across the reachable range, not just one point', () => {
    // A projection that is only correct at the origin is a projection that is
    // wrong where the players are.
    for (const point of [
      [0, 0],
      [77.5946, 12.9716],
      [-122.4194, 37.7749],
      [151.2093, -33.8688],
      [0, -33.8688],
      [0, 60],
    ] as LonLat[]) {
      const [x, y] = lonLatToWorld(point[0], point[1]);
      const [lon, lat] = worldToLonLat(x, y);
      expect(lon).toBeCloseTo(point[0], 6);
      expect(lat).toBeCloseTo(point[1], 6);
    }
  });

  it('puts north at positive y', () => {
    // If this were inverted, the whole map would be upside down and nothing
    // would report an error.
    const [, north] = lonLatToWorld(0, 10);
    const [, south] = lonLatToWorld(0, -10);
    expect(north).toBeGreaterThan(0);
    expect(south).toBeLessThan(0);
  });

  it('is symmetric about the equator', () => {
    const [, a] = lonLatToWorld(0, 20);
    const [, b] = lonLatToWorld(0, -20);
    expect(a).toBeCloseTo(-b, 6);
  });

  it('clamps beyond the Mercator limit instead of diverging', () => {
    // Mercator is infinite at the poles. A player at 89 degrees north is a
    // valid GPS reading, and must not produce an infinite coordinate.
    const [, y] = lonLatToWorld(0, 89);
    expect(Number.isFinite(y)).toBe(true);
    const [lon, lat] = worldToLonLat(0, y);
    expect(lat).toBeLessThanOrEqual(85.05112878);
    expect(lon).toBe(0);
  });
});

describe('hexRing', () => {
  it('returns a closed ring of six corners', () => {
    const ring = hexRing(tile(), SIZE);
    // GeoJSON requires the first and last position to be identical; a ring that
    // is not closed is not a polygon.
    expect(ring).toHaveLength(7);
    expect(ring[0]).toEqual(ring[6]);
  });

  it('places corners one circumradius from the centre', () => {
    const centre = tile();
    const ring = hexRing(centre, SIZE);
    const [cx, cy] = lonLatToWorld(centre.lon, centre.lat);
    for (const [lon, lat] of ring.slice(0, 6)) {
      const [x, y] = lonLatToWorld(lon, lat);
      // Close enough for a float round-trip through degrees and back.
      expect(Math.hypot(x - cx, y - cy)).toBeCloseTo(SIZE, 3);
    }
  });

  it('is pointy-top: no corner due east or west of the centre', () => {
    // The server's geo module is pointy-top, so a corner points north. A
    // flat-top hex would put corners at due east and west, and the two tilings
    // disagree about which hexes are neighbours -- while still leaving no
    // visible gap, which is what makes this easy to ship broken.
    const centre = tile();
    const ring = hexRing(centre, SIZE).slice(0, 6);
    const [cx, cy] = lonLatToWorld(centre.lon, centre.lat);
    const radii = ring.map(([lon, lat]) => {
      const [x, y] = lonLatToWorld(lon, lat);
      return Math.atan2(y - cy, x - cx) * (180 / Math.PI);
    });
    // The nearest corner to due east is 30 degrees away, not 0.
    const eastOffset = Math.min(
      ...radii.map((angle) => Math.abs(normaliseAngle(angle))),
    );
    expect(eastOffset).toBeCloseTo(30, 3);
  });

  it('separates neighbouring hexes by less than the ring diameter', () => {
    // A sanity check on scale: the ring must be a plausible fraction of a
    // degree. 40 m is about 0.00036 degrees of latitude.
    const ring = hexRing(tile(), SIZE);
    const lats = ring.map((p) => p[1]);
    expect(Math.max(...lats) - Math.min(...lats)).toBeGreaterThan(0);
    expect(Math.max(...lats) - Math.min(...lats)).toBeLessThan(0.001);
  });
});

function normaliseAngle(degrees: number): number {
  let angle = degrees;
  while (angle > 180) angle -= 360;
  while (angle < -180) angle += 360;
  return angle;
}

describe('ownershipOf', () => {
  it('reads unclaimed from a null owner', () => {
    expect(ownershipOf(tile(), 1)).toBe('unclaimed');
  });

  it('reads own from a matching id, not a matching name', () => {
    const owned = tile({ owner: { id: 7, display_name: 'sam' } });
    expect(ownershipOf(owned, 7)).toBe('own');
    expect(ownershipOf(owned, 8)).toBe('rival');
  });

  it('treats every tile as rival when nobody is signed in', () => {
    const owned = tile({ owner: { id: 7, display_name: 'sam' } });
    // The public map must not claim any tile belongs to a null viewer.
    expect(ownershipOf(owned, null)).toBe('rival');
  });
});

describe('toFeature', () => {
  it('carries the axial coordinates as properties', () => {
    const feature = toFeature(tile({ q: -3, r: 8 }), 1, SIZE);
    expect(feature.properties.q).toBe(-3);
    expect(feature.properties.r).toBe(8);
  });

  it('uses a stable id so a map engine can diff instead of redraw', () => {
    const feature = toFeature(tile({ q: -3, r: 8 }), 1, SIZE);
    expect(feature.id).toBe('-3,8');
    expect(toFeature(tile({ q: -3, r: 8 }), 1, SIZE).id).toBe(feature.id);
  });

  it('puts lon before lat, as RFC 7946 requires', () => {
    // Reversed, the map renders somewhere plausible but wrong and nothing
    // errors. This is the single easiest thing to get backwards.
    //
    // Checked by the ring's centre of mass rather than by its first corner: a
    // corner is 40 m off the tile centre by definition, so asserting a corner
    // equals the centre would pass for the wrong reason.
    const ring = toFeature(tile(), 1, SIZE).geometry.coordinates[0];
    const meanLon = ring.reduce((sum, p) => sum + p[0], 0) / ring.length;
    const meanLat = ring.reduce((sum, p) => sum + p[1], 0) / ring.length;

    expect(meanLon).toBeCloseTo(CENTRE[0], 3);
    expect(meanLat).toBeCloseTo(CENTRE[1], 3);

    // And the failure this guards against is a swap, not a rounding error:
    // [12.97, 77.59] is nowhere near Bengaluru.
    expect(Math.abs(meanLon - CENTRE[1])).toBeGreaterThan(1);
  });

  it('reports a null owner as null rather than undefined', () => {
    const feature = toFeature(tile(), 1, SIZE);
    expect(feature.properties.ownerId).toBeNull();
    expect(feature.properties.ownerName).toBeNull();
  });
});

describe('worldToLayers', () => {
  const world = {
    centre: { q: 0, r: 0, lat: CENTRE[1], lon: CENTRE[0], world_x: 0, world_y: 0 },
    rings: 1,
    tile_count: 3,
    owned_count: 1,
    hex_size_meters: SIZE,
    viewer_id: 7,
    latitude_was_clamped: false,
    bounds: {
      min_lon: CENTRE[0] - 0.001,
      max_lon: CENTRE[0] + 0.001,
      min_lat: CENTRE[1] - 0.001,
      max_lat: CENTRE[1] + 0.001,
      min_world_x: 0,
      max_world_x: 1,
      min_world_y: 0,
      max_world_y: 1,
    },
    tiles: [
      tile({ q: 0, r: 0, owner: { id: 7, display_name: 'me' } }),
      tile({ q: 1, r: 0, owner: { id: 9, display_name: 'them' } }),
      tile({ q: 0, r: 1 }),
    ],
  };

  it('splits tiles into one collection per ownership', () => {
    const layers = worldToLayers(world);
    expect(layers.own.features).toHaveLength(1);
    expect(layers.rival.features).toHaveLength(1);
    expect(layers.unclaimed.features).toHaveLength(1);
  });

  it('loses no tiles in the split', () => {
    const layers = worldToLayers(world);
    const total =
      layers.own.features.length +
      layers.rival.features.length +
      layers.unclaimed.features.length;
    expect(total).toBe(world.tile_count);
  });

  it('sends everything to rival when nobody is signed in', () => {
    const layers = worldToLayers({ ...world, viewer_id: null });
    expect(layers.own.features).toHaveLength(0);
    expect(layers.unclaimed.features).toHaveLength(1);
    expect(layers.rival.features).toHaveLength(2);
  });
});

describe('includePoint', () => {
  const bounds = { min_lon: 10, max_lon: 20, min_lat: 5, max_lat: 15 };

  it('grows the bounds to contain a point outside them', () => {
    // The player's own marker can land just past the viewport edge, because
    // the server centres on a hex and the player is somewhere inside one.
    const grown = includePoint(bounds, [25, 1]);
    expect(grown.max_lon).toBe(25);
    expect(grown.min_lat).toBe(1);
  });

  it('leaves the bounds alone for a point already inside', () => {
    expect(includePoint(bounds, [15, 10])).toEqual(bounds);
  });
});

describe('tileCentre', () => {
  it('returns lon before lat', () => {
    expect(tileCentre(tile())).toEqual([CENTRE[0], CENTRE[1]]);
  });
});
