import { useCallback, useEffect, useRef, useState } from 'react';
import { Pedometer } from 'expo-sensors';
import { useQueryClient } from '@tanstack/react-query';

import { api } from '../api/runtime';
import { syncSteps } from '../api/mana';
import { ApiError } from '../api/errors';
import { useSession } from '../auth/session';
import {
  batchToSend,
  commitSync,
  EMPTY_LEDGER,
  hasStepsToSync,
  observe,
  stepsAwaitingSync,
  type StepLedger,
} from './checkpoint';
import { clearLedger, loadLedger, saveLedger } from './ledgerStore';

/**
 * Neither the result type nor the callback type is re-exported from the package
 * index, so both are derived from the public function signature. Deep-importing
 * `expo-sensors/build/Pedometer` would be a path into a build directory, which
 * is not part of the package's public surface and can move between versions.
 */
type PedometerUpdate = Parameters<typeof Pedometer.watchStepCount>[0];
type PedometerResult = Parameters<PedometerUpdate>[0];

export type StepTrackerStatus =
  /** Still reading the stored ledger. */
  | 'loading'
  /** The device has no pedometer, so no steps will ever be recorded. */
  | 'unavailable'
  /** Watched, and steps are being accumulated. */
  | 'tracking'
  /** Watched, but the player declined the motion permission. */
  | 'denied';

export interface StepTracker {
  status: StepTrackerStatus;
  /** Steps observed but not yet accepted by the server. */
  pendingSteps: number;
  lastSyncedAt: number | null;
  /** Report the pending steps, resolving to the server's authoritative mana. */
  sync: () => Promise<number>;
  syncing: boolean;
  error: string | null;
}

/**
 * Accumulate walked steps and hand them to the server.
 *
 * All the arithmetic is delegated to `./checkpoint`, which is pure and tested.
 * This hook is only the wiring -- subscription, persistence, and the request --
 * because that part cannot be tested without a device, and the part that
 * decides whether a player gets paid correctly should not be the untested part.
 */
export function useStepTracker(): StepTracker {
  const queryClient = useQueryClient();
  const userId = useSession((state) => state.user?.id ?? null);
  const accountKey = userId === null ? null : String(userId);

  // The ledger is stored alongside the account it belongs to, so that switching
  // accounts cannot show -- let alone submit -- the other player's steps. Holding
  // the key in the same state object lets the visible ledger be derived during
  // render, which avoids both a flash of the previous account's balance and a
  // setState inside an effect.
  const [stored, setStored] = useState<{ accountKey: string | null; ledger: StepLedger }>({
    accountKey,
    ledger: EMPTY_LEDGER,
  });
  const ledger = stored.accountKey === accountKey ? stored.ledger : EMPTY_LEDGER;

  const [status, setStatus] = useState<StepTrackerStatus>('loading');
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Mirrored in a ref because the pedometer callback must not resubscribe
  // whenever the ledger changes: the subscription is long-lived, and a fresh
  // one per reading would be constant native churn.
  const ledgerRef = useRef<StepLedger>(ledger);

  const applyLedger = useCallback(
    (key: string, next: StepLedger) => {
      ledgerRef.current = next;
      setStored({ accountKey: key, ledger: next });
    },
    [],
  );

  // Restore the stored ledger, then watch the pedometer.
  useEffect(() => {
    if (accountKey === null) {
      return;
    }

    let cancelled = false;
    let subscription: { remove: () => void } | null = null;

    // The account key is passed in rather than captured, so its non-null type
    // is guaranteed inside the async function regardless of when it is awaited.
    async function start(key: string): Promise<void> {
      const restored = await loadLedger(key);
      if (cancelled) {
        return;
      }
      ledgerRef.current = restored;
      setStored({ accountKey: key, ledger: restored });

      const available = await Pedometer.isAvailableAsync();
      if (cancelled) {
        return;
      }
      if (!available) {
        setStatus('unavailable');
        return;
      }

      const permission = await Pedometer.requestPermissionsAsync();
      if (cancelled) {
        return;
      }
      if (!permission.granted) {
        setStatus('denied');
        return;
      }

      subscription = Pedometer.watchStepCount((result: PedometerResult) => {
        const next = observe(ledgerRef.current, result.steps);
        ledgerRef.current = next;
        setStored({ accountKey: key, ledger: next });
        // Persisted on every reading, not on sync, so being killed mid-walk does
        // not lose the steps. `watchStepCount` batches rather than firing once
        // per step, so this is not a write per stride.
        void saveLedger(key, next);
      });
      setStatus('tracking');
    }

    void start(accountKey);

    return () => {
      cancelled = true;
      subscription?.remove();
    };
  }, [accountKey]);

  const sync = useCallback(async (): Promise<number> => {
    if (accountKey === null) {
      throw new Error('Cannot sync steps before signing in');
    }
    if (!hasStepsToSync(ledgerRef.current)) {
      return 0;
    }
    const batch = batchToSend(ledgerRef.current);

    setSyncing(true);
    setError(null);
    try {
      const response = await syncSteps(api, { steps_delta: batch });
      // Only now, on a 2xx, is it safe to forget these steps. Committing before
      // the response arrives is how a walk gets lost for good.
      const next = commitSync(ledgerRef.current, batch, Date.now());
      applyLedger(accountKey, next);
      await saveLedger(accountKey, next);
      // The sync response carries authoritative mana, but the header reads the
      // `me` query, so it is invalidated rather than reconstructed here. One
      // request, and the two views cannot disagree.
      await queryClient.invalidateQueries({ queryKey: ['me'] });
      return response.mana;
    } catch (cause) {
      // Left pending on purpose: the next attempt resends the same walk. The
      // failure most players will hit is the walking-speed check rejecting a
      // second sync soon after the first, which reads as nonsense unless it is
      // translated -- so the API's own user-facing wording is preferred over the
      // raw detail, which names internal concepts like a "delta".
      setError(describeSyncFailure(cause));
      throw cause;
    } finally {
      setSyncing(false);
    }
  }, [accountKey, applyLedger, queryClient]);

  return {
    status,
    pendingSteps: stepsAwaitingSync(ledger),
    lastSyncedAt: ledger.lastSyncedAt,
    sync,
    syncing,
    error,
  };
}

export { clearLedger };

function describeSyncFailure(cause: unknown): string {
  if (cause instanceof ApiError) {
    return cause.userMessage;
  }
  if (cause instanceof Error) {
    return 'Could not report your steps. They are saved and will go through next time.';
  }
  return 'Could not report your steps.';
}
