/** Claiming territory. */

import type { ApiClient } from './client';
import type { ClaimByLocationRequest, ClaimResponse } from './types';

/**
 * Claim one hex by its axial coordinates.
 *
 * What a tap on the map sends. The `(q, r)` pair comes straight from
 * `GET /world`, so the client never has to convert a tap position into a hex
 * itself -- the server owns that projection, and duplicating it here is how two
 * devices end up disagreeing about which hex a point is in.
 */
export function claim(
  client: ApiClient,
  coords: { q: number; r: number },
): Promise<ClaimResponse> {
  return client.post<ClaimResponse>('/territory/claim', {
    q: coords.q,
    r: coords.r,
  });
}

/**
 * Claim whichever hex contains a GPS fix.
 *
 * What a "claim the ground I'm standing on" button sends. Sending raw
 * coordinates and letting the server project them is the same reasoning as
 * above: the answer cannot disagree with the map.
 *
 * Needs a real fix. A stale or mocked location would claim a tile far from the
 * player, and the leash may or may not catch it depending on how far, so the
 * caller is responsible for checking the fix's age before calling this.
 */
export function claimByLocation(
  client: ApiClient,
  point: ClaimByLocationRequest,
): Promise<ClaimResponse> {
  return client.post<ClaimResponse>('/territory/claim-by-location', {
    lat: point.lat,
    lon: point.lon,
  });
}
