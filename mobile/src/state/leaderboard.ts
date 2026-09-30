/**
 * The leaderboard query.
 *
 * A single react-query entry point for the one "read a page you can keep
 * returning to" screen in the app. Kept here rather than on the screen so the
 * screen stays a renderer, and so a later tab that wants the same board shares
 * the cache instead of fetching a second copy.
 */

import { useQuery } from '@tanstack/react-query';

import { api } from '../api/runtime';
import { getLeaderboard } from '../api/leaderboard';
import { useSession } from '../auth/session';

export const leaderboardKey = (limit: number) => ['leaderboard', limit] as const;

/**
 * The top players, refreshed only when the screen is worth a round trip.
 *
 * The board moves only when someone claims a hex, which is not something that
 * happens while the reader is looking at it -- so the stale window is longer
 * than the map's, and there is no polling.
 */
export function useLeaderboard(limit = 50) {
  const { status } = useSession();

  return useQuery({
    queryKey: leaderboardKey(limit),
    queryFn: () => getLeaderboard(api, limit),
    enabled: status === 'signed-in',
    staleTime: 60_000,
  });
}