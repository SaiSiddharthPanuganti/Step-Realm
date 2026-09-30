import { useCallback, useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import { api } from '../api/runtime';
import { walkReport } from '../api/territory';
import { ApiError } from '../api/errors';
import {
  commitReports,
  emptyFallbackRun,
  emptyLedger,
  fallbackMoving,
  hasSpeed,
  isMovingFix,
  observeSegment,
  paceMeters,
  pendingFor,
  reportedFor,
  SPEED_DECISION_FIXES,
  toReports,
  type FallbackRunState,
  type WalkLedger,
} from './accumulate';

/**
 * Turn a stream of GPS fixes into banked walk metres.
 *
 * All the arithmetic lives in `./accumulate`, which is pure and tested; this
 * hook is only the wiring. On every new fix it credits the step's earned
 * metres to the hex its start fell in, and on a timer it flushes the pending
 * buckets to the server -- in `POST /territory/walk`'s terms, "here is how far
 * I have walked since the last time I told you".
 *
 * Which of the two measurement modes the tracker uses is decided per session,
 * from the first handful of fixes:
 *
 * * Speed mode: the chip reports speed, so a step's metres are the reported
 *   speed held over the measured time (`paceMeters`). Position noise never
 *   enters the number, so a phone that is standing still earns nothing no
 *   matter how much the fix point drifts.
 * * Fallback mode: the chip is silent about speed, so metres come from
 *   position only when a run of same-direction hops proves real motion
 *   (`fallbackMoving`). Rare on any Google-flavoured Android, kept so a silent
 *   provider still banks a walk.
 *
 * The ledger is deliberately in-memory only. The server is the ledger of
 * record (the same rule as steps): a session restart loses only the metres
 * that were still pending a flush, never the ones it reported. Bumping that to
 * disk persistence buys nothing until the walk tracker survives a backgrounded
 * app, which the GPS watch does not anyway.
 */

export interface WalkTracker {
  /** Pending (unflushed) metres in one hex, for the HUD's live total. */
  pendingFor: (q: number, r: number) => number;
  /** Confirmed metres the server accepted for one hex, since this session. */
  reportedFor: (q: number, r: number) => number;
  /** Send every pending bucket, resolving when they are all accepted. */
  flush: () => Promise<void>;
  reporting: boolean;
  /** Why the last flush failed, in player language. Non-fatal. */
  error: string | null;
}

/** A failed report is a hint, not a dead stop: pending metres stay for the
 *  next flush. This is the only user-facing failure the tracker can produce,
 *  so it names the walk, not the network stack. */
function describeFlushFailure(cause: unknown): string {
  if (cause instanceof ApiError) {
    return cause.userMessage;
  }
  return 'Walking progress could not be sent. It is saved and will go through next time.';
}

export function useWalkTracker(input: {
  latitude: number | null;
  longitude: number | null;
  /** Metres per second as reported by the chip, when it reports one. */
  speed: number | null;
  /** Epoch milliseconds of the fix, for the `speed * time` integration. */
  timestamp: number | null;
  enabled: boolean;
  hexSizeMeters: number;
}): WalkTracker {
  const { latitude, longitude, speed, timestamp, enabled, hexSizeMeters } = input;
  const queryClient = useQueryClient();

  const [ledger, setLedger] = useState<WalkLedger>(emptyLedger);
  const [reporting, setReporting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Mirrored in a ref because the position effect and the flush timer must
  // read the latest ledger without resubscribing whenever it changes.
  const ledgerRef = useRef<WalkLedger>(ledger);
  const applyLedger = useCallback((next: WalkLedger) => {
    ledgerRef.current = next;
    setLedger(next);
  }, []);

  // Whether the chip can measure speed at all. `null` while undecided (the
  // first few fixes), then locked for the session. Decided once because a
  // provider that measures speed reports it every fix; a provider that cannot
  // never will, and flipping between modes would bank two versions of the
  // same walk.
  const modeRef = useRef<'speed' | 'fallback' | null>(null);
  const speedingRef = useRef(false);
  // Counts fixes while the speed decision is still open. A ref so the effect
  // keeps it across renders without resubscribing; ignored once the mode
  // locks.
  const decidingFixesRef = useRef(0);

  // The anchor of the current moving phase: the last fix a step was measured
  // from. Reset to null whenever the player stops, so the first step after a
  // pause measures from its own start and cannot inherit stale metres.
  const stepRef = useRef<{
    lat: number;
    lon: number;
    timestamp: number;
  } | null>(null);

  // Fallback-mode state: the running count of same-direction hops.
  const fallbackRef = useRef<FallbackRunState>(emptyFallbackRun());

  // Credit each new fix. Runs on every position update from the watch; stays
  // a no-op while the player stands still, because a stationery phone earns
  // nothing and its drift is deliberately never banked.
  useEffect(() => {
    if (!enabled || latitude === null || longitude === null) {
      return;
    }
    if (modeRef.current === null) {
      if (hasSpeed(speed)) {
        speedingRef.current = true;
      }
      if (speedingRef.current || ++decidingFixesRef.current >= SPEED_DECISION_FIXES) {
        modeRef.current = speedingRef.current ? 'speed' : 'fallback';
      } else {
        return;
      }
    }

    if (modeRef.current === 'speed') {
      if (!isMovingFix(speed)) {
        // Standing still (or an implausible rush). Drop the phase anchor so
        // the next moving step starts measuring from a clean reference.
        stepRef.current = null;
        return;
      }
      if (timestamp === null || timestamp === undefined) {
        return;
      }
      const previous = stepRef.current;
      if (previous === null) {
        stepRef.current = { lat: latitude, lon: longitude, timestamp };
        return;
      }
      if (timestamp < previous.timestamp) {
        // A time went backwards (clock step, stale fix): restart the phase.
        stepRef.current = { lat: latitude, lon: longitude, timestamp };
        return;
      }
      const dtSeconds = (timestamp - previous.timestamp) / 1000;
      if (dtSeconds <= 0) {
        stepRef.current = { lat: latitude, lon: longitude, timestamp };
        return;
      }
      const meters = paceMeters(speed as number, dtSeconds);
      if (meters === null) {
        // A gap longer than we measured (a dead radio silently passing
        // `MAX_PACE_STEP_SECONDS`): restart rather than invent the missing
        // stretch.
        stepRef.current = { lat: latitude, lon: longitude, timestamp };
        return;
      }
      applyLedger(
        observeSegment(ledgerRef.current, { lat: previous.lat, lon: previous.lon }, meters, hexSizeMeters),
      );
      stepRef.current = { lat: latitude, lon: longitude, timestamp };
      return;
    }

    // Fallback mode: no speed measurement, so position plus a same-direction
    // run is the only honest motion signal.
    const previous = stepRef.current;
    if (previous === null) {
      stepRef.current = { lat: latitude, lon: longitude, timestamp: timestamp ?? 0 };
      return;
    }
    const verdict = fallbackMoving(
      fallbackRef.current,
      { lat: previous.lat, lon: previous.lon },
      { lat: latitude, lon: longitude },
    );
    fallbackRef.current = verdict.nextState;
    if (verdict.moving && verdict.meters > 0) {
      applyLedger(
        observeSegment(ledgerRef.current, { lat: previous.lat, lon: previous.lon }, verdict.meters, hexSizeMeters),
      );
    }
    stepRef.current = { lat: latitude, lon: longitude, timestamp: timestamp ?? 0 };
  }, [latitude, longitude, speed, timestamp, enabled, hexSizeMeters, applyLedger]);

  // One in-flight flush at a time, so a slow network cannot pile requests up
  // and so the interval cannot fire into the claim-time flush.
  const flushingRef = useRef(false);

  const flush = useCallback(async (): Promise<void> => {
    if (!enabled || flushingRef.current) {
      return;
    }
    const reports = toReports(ledgerRef.current);
    if (reports.length === 0) {
      return;
    }
    flushingRef.current = true;
    setReporting(true);
    setError(null);
    const accepted: { q: number; r: number }[] = [];
    try {
      for (const report of reports) {
        // The server projects the coordinate and answers with `total`, which
        // the HUD does not need -- the world poll carries the authoritative
        // numbers, and adding a second source of truth is how they drift.
        await walkReport(api, report);
        accepted.push({ q: report.q, r: report.r });
      }
      if (accepted.length > 0) {
        applyLedger(commitReports(ledgerRef.current, accepted));
      }
      // The world poll would surface the new totals within its 20 s window
      // anyway; nudging it means a HUD reading after a flush is never stale.
      await queryClient.invalidateQueries({ queryKey: ['world'] });
    } catch (cause) {
      setError(describeFlushFailure(cause));
    } finally {
      flushingRef.current = false;
      setReporting(false);
    }
  }, [enabled, applyLedger, queryClient]);

  // The flush cadence. Five seconds is short enough that a player who walks
  // out of a hex quickly still banks most of it in the hex it was earned in,
  // and long enough that the request never outruns the plausibility check the
  // server applies to each one.
  useEffect(() => {
    if (!enabled) {
      return;
    }
    const timer = setInterval(() => {
      void flush();
    }, 5_000);
    return () => clearInterval(timer);
  }, [enabled, flush]);

  return {
    pendingFor: (q, r) => pendingFor(ledgerRef.current, q, r),
    reportedFor: (q, r) => reportedFor(ledgerRef.current, q, r),
    flush,
    reporting,
    error,
  };
}