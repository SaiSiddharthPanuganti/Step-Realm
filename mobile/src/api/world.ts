/** Reading the world viewport. */

import type { ApiClient } from './client';
import type { WorldResponse } from './types';

export interface WorldQuery {
  lat?: number;
  lon?: number;
  q?: number;
  r?: number;
  rings?: number;
}

/**
 * Fetch the hexes around a position.
 *
 * Takes the stored token when there is one, but not as a requirement: this
 * endpoint is public and answers `viewer_id: null` for an anonymous caller.
 * That is deliberate, so the map can be browsed before signing up and so a
 * player whose session has expired can still see the world instead of a blank
 * screen.
 *
 * A position outside the player's world leash comes back as a 422 from a
 * centre that was already clamped into range, and the caller should surface
 * `latitude_was_clamped` rather than pretend it got what it asked for.
 */
export function getWorld(
  client: ApiClient,
  query: WorldQuery = {},
  signal?: AbortSignal,
): Promise<WorldResponse> {
  return client.request<WorldResponse>('/world', {
    method: 'GET',
    query: {
      lat: query.lat,
      lon: query.lon,
      q: query.q,
      r: query.r,
      rings: query.rings,
    },
    signal,
  });
}
