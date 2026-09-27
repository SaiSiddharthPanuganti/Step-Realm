/** Reporting walked steps. */

import type { ApiClient } from './client';
import type { ManaSyncRequest, ManaSyncResponse } from './types';

/**
 * Report steps walked since the last successful sync.
 *
 * The caller must pass a *delta*. The server bounds the value it accepts, and
 * the daily mana cap bounds what any count can be worth, so a client that
 * miscounts does not break the economy -- but it does hand out free mana, so
 * the delta discipline is enforced here rather than left to the call site.
 *
 * The response is complete mana state rather than a delta, and the caller
 * should replace its local model with it wholesale. Reconstructing the balance
 * locally is how a client ends up showing a mana bar the server disagrees
 * with, usually after a period offline.
 */
export function syncSteps(
  client: ApiClient,
  request: ManaSyncRequest,
): Promise<ManaSyncResponse> {
  return client.post<ManaSyncResponse>('/mana/sync', {
    steps_delta: request.steps_delta,
  });
}
