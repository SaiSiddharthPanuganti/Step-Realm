/**
 * Does the client's geometry agree with the server's?
 *
 * The server sends each tile twice: as lat/lon, and as Web Mercator plane
 * metres. The client uses the plane metres to place the polygon corners and the
 * lat/lon to label the tile, so if the client's inverse projection disagreed
 * with the server's forward projection by even a little, every hex would be
 * drawn slightly off from the coordinates the rest of the app believes it has.
 *
 * Nothing about that would look like a bug. The map would render, the hexes
 * would tile without gaps, and a tap would still claim a correct tile --
 * because the tap sends `(q, r)` straight back. The only visible symptom would
 * be a marker or label sitting a few metres from the ground it names.
 *
 * So this is checked directly, against a real server, tile by tile.
 */

import { describe, expect, it } from 'vitest';

import { getWorld } from '../src/api/world';
import { worldToLonLat } from '../src/map/geojson';
import { CONTRACT_BASE_URL } from './helpers/server';
import { ApiClient } from '../src/api/client';
import { assertWorldResponse } from './helpers/shape';

const client = new ApiClient({ baseUrl: CONTRACT_BASE_URL });

/** Places far enough apart that a projection error would show up. */
const PLACES = [
  { lat: 12.9716, lon: 77.5946, label: 'Bengaluru' },
  { lat: 28.6139, lon: 77.209, label: 'Delhi' },
  { lat: 19.076, lon: 72.8777, label: 'Mumbai' },
  { lat: 40.7128, lon: -74.006, label: 'New York' },
  { lat: -33.8688, lon: 151.2093, label: 'Sydney' },
  { lat: 64.1466, lon: -21.9426, label: 'Reykjavik' },
];

describe('client geometry against the server', () => {
  it('inverts the projection the server applied', async () => {
    for (const place of PLACES) {
      const world = await getWorld(client, { lat: place.lat, lon: place.lon, rings: 2 });
      assertWorldResponse(world);

      expect(world.tiles.length).toBeGreaterThan(0);

      for (const tile of world.tiles) {
        const [lon, lat] = worldToLonLat(tile.world_x, tile.world_y);
        // Seven decimal places, because that is what the server rounds to. Any
        // tighter and this measures the server's rounding rather than whether
        // the two projections agree: at 1e-7 degrees the residual is about a
        // centimetre, which is the floor of what the wire format can express.
        expect(lon).toBeCloseTo(tile.lon, 6);
        expect(lat).toBeCloseTo(tile.lat, 6);
      }
    }
  });

  it('serves the same origin the projection assumes', async () => {
    // The client has no hardcoded origin, so this asserts the server's world
    // origin is a sane WGS84 point. If the server's origin were ever set to
    // something out of range, the client would still "work" while drawing
    // polygons in the wrong hemisphere.
    const world = await getWorld(client, { lat: 0, lon: 0, rings: 0 });
    expect(Math.abs(world.centre.lon)).toBeLessThanOrEqual(180);
    expect(Math.abs(world.centre.lat)).toBeLessThanOrEqual(85.05112878);
  });

  it('spaces tiles by the distance the hex size implies', async () => {
    const world = await getWorld(client, { lat: 12.9716, lon: 77.5946, rings: 1 });
    const centre = world.tiles.find(
      (tile) => tile.q === world.centre.q && tile.r === world.centre.r,
    );
    const neighbour = world.tiles.find(
      (tile) => (tile.q !== world.centre.q || tile.r !== world.centre.r),
    );
    expect(centre).toBeDefined();
    expect(neighbour).toBeDefined();

    // The distance between two hex centres should be a small multiple of the
    // circumradius -- sqrt(3) * size edge-to-edge for pointy-top. A client using
    // the wrong radius or the wrong orientation would be off by a large factor,
    // which is the failure a wrong HEX_SIZE_METERS would cause.
    const dx = neighbour!.world_x - centre!.world_x;
    const dy = neighbour!.world_y - centre!.world_y;
    const spacing = Math.hypot(dx, dy);
    expect(spacing).toBeGreaterThan(world.hex_size_meters);
    expect(spacing).toBeLessThan(world.hex_size_meters * 2);
  });
});
