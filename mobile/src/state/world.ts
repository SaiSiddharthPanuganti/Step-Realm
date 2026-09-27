/**
 * Server state.
 *
 * React Query rather than hand-rolled effects, because the interesting part of
 * this app is that several screens care about the same balance and the same
 * tiles, and a second copy of either is a second thing to forget to update.
 *
 * The rule applied throughout: the server's answer replaces the local model
 * wholesale. Never apply a delta. Mana and territory both change as a result of
 * things happening elsewhere -- another player claiming a tile, a passive
 * regen tick -- and a locally-adjusted balance is wrong the moment any of those
 * happen, usually without anything looking broken.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '../api/runtime';
import { getWorld } from '../api/world';
import { claim as claimRequest, claimByLocation as claimByLocationRequest } from '../api/territory';
import { syncSteps as syncStepsRequest } from '../api/mana';
import { me as fetchMe } from '../api/auth';
import { ApiError } from '../api/errors';
import type { ClaimByLocationRequest } from '../api/types';
import type { WorldQuery } from '../api/world';
import { useSession } from '../auth/session';

/** Where the map is looking. Part of the world query's identity. */
export interface Viewport extends WorldQuery {
  lat: number;
  lon: number;
}

export const worldKey = (viewport: Viewport) => ['world', viewport] as const;

/**
 * Polling interval for the map.
 *
 * Slow on purpose. The viewport is 7 to 127 tiles, and the player is walking --
 * not stationary in a browser tab. Refetching every few seconds would spend the
 * player's battery and the rate limit to redraw a map they are not looking at
 * closely. Territory changes are also visible the moment the player acts on
 * them, because the claim response carries the new state.
 */
const WORLD_REFETCH_MS = 20_000;

export function useWorld(viewport: Viewport | null) {
  const { status } = useSession();

  return useQuery({
    queryKey: worldKey(viewport ?? { lat: 0, lon: 0 }),
    queryFn: ({ signal }) => getWorld(api, viewport ?? {}, signal),
    // Without a viewport there is nothing to ask for, and a request centred on
    // null island would be a confusing 200 rather than an obvious mistake.
    enabled: viewport !== null && status === 'signed-in',
    refetchInterval: WORLD_REFETCH_MS,
    // Territory is shared and contested, so a stale map is worse than a brief
    // spinner. Showing the previous viewport while the next loads keeps the map
    // from flashing empty on every pan.
    placeholderData: (previous) => previous,
    retry: (failureCount, error) => {
      // Retrying a 422 or a 409 will never succeed; retrying a dropped
      // connection usually will.
      if (error instanceof ApiError && error.isGameRule) {
        return false;
      }
      if (error instanceof ApiError && error.isRateLimited) {
        // The limiter is telling us to wait, so back off for as long as it
        // asked rather than hammering it and extending the window.
        return failureCount < 2;
      }
      return failureCount < 3;
    },
  });
}

/**
 * Claim a hex.
 *
 * On success the world and the profile are both refetched rather than patched.
 * A claim changes the mana balance, the owned-tile count, and the ownership of
 * one tile -- and potentially of a neighbour, if a rival claimed it between the
 * map load and the tap. Patching the three locally is a guess; refetching is a
 * question the server answers correctly.
 */
export function useClaim() {
  const queryClient = useQueryClient();
  const updateUser = useSession((state) => state.updateUser);

  return useMutation({
    mutationFn: (coords: { q: number; r: number }) => claimRequest(api, coords),
    onSuccess: async (result) => {
      // The claim response is authoritative about the balance, the cap and the
      // owned-tile count, so patch exactly those three and let the /me refetch
      // confirm the rest.
      const current = useSession.getState().user;
      if (current !== null) {
        updateUser({
          ...current,
          mana: result.mana,
          mana_max: result.mana_max,
          owned_tile_count: result.owned_tile_count,
        });
      }
      await queryClient.invalidateQueries({ queryKey: ['world'] });
      await queryClient.invalidateQueries({ queryKey: ['me'] });
    },
  });
}

export function useClaimByLocation() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (point: ClaimByLocationRequest) => claimByLocationRequest(api, point),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['world'] });
      await queryClient.invalidateQueries({ queryKey: ['me'] });
    },
  });
}

/**
 * Report walked steps.
 *
 * The delta must be steps since the last *successful* sync. A retried sync after
 * a timeout would otherwise credit the same walk twice, and a client that
 * sends a running total instead would credit it on every call.
 */
export function useSyncSteps() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (stepsDelta: number) => syncStepsRequest(api, { steps_delta: stepsDelta }),
    onSuccess: async () => {
      // The sync response is complete mana state, but the session store's user
      // is a different shape. Rather than duplicate the merge, let /me supply
      // it -- the response told us the balance changed, and this is one request.
      await queryClient.invalidateQueries({ queryKey: ['me'] });
    },
  });
}

/** The profile, refetched on the same cadence as the map. */
export function useProfile() {
  const { status } = useSession();

  return useQuery({
    queryKey: ['me'],
    queryFn: () => fetchMe(api),
    enabled: status === 'signed-in',
    staleTime: 30_000,
  });
}
