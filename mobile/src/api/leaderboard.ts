/** The leaderboard: players ranked by territory claimed. */

import type { ApiClient } from './client';
import type { LeaderboardResponse } from './types';

/**
 * Fetch the top players by tiles claimed.
 *
 * Authenticated, because the answer includes the caller's own standing as
 * the `viewer` entry -- a leaderboard with your row in it is a private
 * mirror of a public ranking. The `degraded` flag says whether the server
 * could serve it from Redis or had to recompute from Postgres; the ranking
 * is the same either way.
 */
export function getLeaderboard(
  client: ApiClient,
  limit = 50,
): Promise<LeaderboardResponse> {
  return client.request<LeaderboardResponse>('/leaderboard', {
    method: 'GET',
    query: { limit },
  });
}