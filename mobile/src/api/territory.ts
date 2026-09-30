/** Claiming territory. */

import type { ApiClient } from './client';
import type {
  ClaimByLocationRequest,
  ClaimResponse,
  WalkReportRequest,
  WalkReportResponse,
} from './types';

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

/**
 * Bank walked metres into the hex the server places them in.
 *
 * The client sends a coordinate and a delta, nothing else. Projecting the
 * coordinate to a hex is the server's job -- the whole reason the client never
 * decides where a metre landed is that two projections would eventually disagree
 * about it. The coordinate is the *start* of the reported segment, so a metre
 * crossing a hex border is credited to the hex the walking began in.
 */
export function walkReport(
  client: ApiClient,
  report: WalkReportRequest,
): Promise<WalkReportResponse> {
  return client.post<WalkReportResponse>('/territory/walk', {
    lat: report.lat,
    lon: report.lon,
    meters: report.meters,
  });
}
