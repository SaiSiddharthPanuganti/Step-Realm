/**
 * The arithmetic behind the walk tracker, kept pure and dependency-free so it
 * can be tested in Node.
 *
 * Responsibility split, mirroring the step tracker: this module decides how
 * raw GPS fixes become per-hex metre buckets, and `useWalkTracker` is only the
 * wiring -- the subscription, the timer, and the requests. The part that
 * decides whether a walk is measured correctly should not be the untested
 * part.
 *
 * Rules enforced here:
 *
 * * A fix is movement only when the chip's own speed measurement says so. GPS
 *   position is accurate to metres, not centimetres, so the distance between
 *   two consecutive fixes is mostly error: a still phone "drifts" 3-8 m while
 *   a walking phone's true 1 m/s step is invisible under 10-20 m of noise.
 *   The provider's speed, on the other hand, is doppler-smoothed and reliable
 *   to a fraction of a metre per second -- so distance is `speed * time`
 *   integrated over the measured steps, not the length of a position hop.
 * * `isMovingFix` gates on that speed: below the walking floor it is
 *   stationary jitter (a standing phone reports ~0.3 m/s), above the tracking
 *   cap it is implausible for a game that promises walking (the server applies
 *   the same cap). Only fixes in between earn metres.
 * * A step's metres are capped at `MAX_PACE_STEP_SECONDS` of speed. A pause
 *   longer than that is time the app did not measure; crediting the whole span
 *   would mint distance out of a radio glitch, so the step is restarted
 *   instead.
 * * A segment is credited to the hex the player was *in at its start*. That is
 *   the coordinate sent to the server, so the client and server cannot
 *   disagree about where a metre landed even at a hex border.
 * * Some chips never report speed at all. `fallbackMoving` is the negotiating
 *   position for those: a hop only earns once two hops in a row push in the
 *   same direction, which a still phone's random walk does not -- it rears its
 *   head in one hop and retreats in the next.
 * * Pending metres flush only when they clear `REPORT_FLOOR_METERS`, so a
 *   player standing at a border does not push a 0.3 m report at the server.
 * * A report carries the pending count to one decimal place, because floating
 *   point wrote 14.799999999999 to the wire for a user who walked exactly
 *   14.8 m.
 */

import { bearingDegrees, haversineMeters, lonlatToAxial } from '../map/geojson';

/**
 * A still phone's reported wander; below this there is no walking.
 *
 * Deliberately at 1.0 rather than the "already walking" 1.4: cheap doppler
 * speed is smoothed, so a standing phone sits comfortably under half a metre
 * per second, while the same chips can throw momentary 0.8-0.9 m/s readings at
 * idle. 1.0 clears both without making a gentle stroll impossible.
 */
export const WALK_SPEED_FLOOR_METERS_PER_SECOND = 1.0;

/** The same 4 m/s the server refuses at speed; the game promises walking. */
export const MAX_TRACK_SPEED_METERS_PER_SECOND = 4.0;

/**
 * A fix gap longer than this is time the app did not measure. A step through a
 * longer gap is not credited (the phase restarts instead), because `speed *
 * gap` would invent distance for the part of the gap that has no fix to say
 * the player was actually walking.
 */
export const MAX_PACE_STEP_SECONDS = 3;

/** How many fixes without a speed prove the chip cannot report one. */
export const SPEED_DECISION_FIXES = 6;

/** Movement smaller than this is jitter, used only in the no-speed fallback. */
export const MIN_TRACK_DELTA_METERS = 2;

/**
 * How far a stationary fix may pull the on-screen position from the last
 * released one before it is treated as an escape rather than jitter.
 *
 * A standing phone's fix point rocks a few metres; beyond this the position
 * really did move, so the marker re-anchors once instead of staying frozen
 * metres away from the player.
 */
export const DRIFT_ESCAPE_METERS = 4;

/** Stationary fixes within this of each other are one "still spell". */
export const STILL_CLUSTER_METERS = 3;

/** Consecutive still fixes that prove a stop, after which the marker settles. */
export const STILL_SETTLE_FIXES = 3;

/**
 * State of a still spell, so the drift filter can tell "standing, settling"
 * from "standing, already settled" without touching the GPs at all.
 */
export interface Stillness {
  /** Consecutive stationary fixes still clustered around `anchor`. */
  streak: number;
  /** The first fix of the current still spell, the point the marker settles on. */
  anchor: { lat: number; lon: number } | null;
}

export function emptyStillness(): Stillness {
  return { streak: 0, anchor: null };
}

export interface DriftFilterInput {
  /** The chip's speed for this fix, if it reported one. */
  speed: number | null;
  /** Stillness state carried across fixes. */
  stillness: Stillness;
  /** The raw fix. */
  fix: { lat: number; lon: number };
  /** The position currently on screen (the last released fix). */
  released: { lat: number; lon: number } | null;
}

export interface DriftFilterVerdict {
  /** The position the player should be shown at now. */
  released: { lat: number; lon: number };
  /** Stillness state for the next fix. */
  stillness: Stillness;
  /** Whether the marker should move to this fix (vs holding its position). */
  follow: boolean;
}

/**
 * Decide whether a raw fix should move the on-screen position, or be held.
 *
 * This is the "drift is not movement" rule for the marker and the hex ring. A
 * standing phone's fix point rocks several metres while the chip's speed stays
 * at a couple of tenths; without this filter that wander would flick the marker
 * and the walk-ring across hex boundaries while the player does not move an
 * inch. So:
 *
 * * The chip's speed is the arbiter: above the walking floor the fix is real
 *   movement and released; below it the player is standing, and the marker
 *   holds.
 * * A still fix may still be an escape (the player walked while the chip was
 *   silent and then stopped): once it strays past `DRIFT_ESCAPE_METERS` it is
 *   released once, so the marker cannot rest metres away from the truth.
 * * Otherwise the still spell banks the first fix as its anchor, and once
 *   `STILL_SETTLE_FIXES` consecutive fixes stay within the cluster the marker
 *   settles on that anchor -- one clean move at the end of a walk, then frozen.
 *
 * The same verdict feeds the walk-gate's hex ring, keeping "which tile am I
 * standing in" as stable as the marker itself.
 */
export function driftFilter(input: DriftFilterInput): DriftFilterVerdict {
  const { speed, stillness, fix, released } = input;
  if (released === null) {
    // The very first fix: nothing was on screen, so there is nothing to hold.
    return { released: fix, stillness: emptyStillness(), follow: true };
  }
  if (isMovingFix(speed)) {
    return { released: fix, stillness: emptyStillness(), follow: true };
  }
  const displaced = haversineMeters([released.lon, released.lat], [fix.lon, fix.lat]);
  if (displaced > DRIFT_ESCAPE_METERS) {
    return { released: fix, stillness: emptyStillness(), follow: true };
  }
  const anchor = stillness.anchor ?? fix;
  const withinCluster =
    haversineMeters([anchor.lon, anchor.lat], [fix.lon, fix.lat]) <= STILL_CLUSTER_METERS;
  const nextAnchor = withinCluster ? anchor : fix;
  const streak = withinCluster ? stillness.streak + 1 : 1;
  const settled = withinCluster && streak >= STILL_SETTLE_FIXES;
  return {
    released: settled ? nextAnchor : released,
    stillness: { streak, anchor: nextAnchor },
    // Move the marker once, when the stop is proven; the still spell then
    // holds that settled position instead of re-announcing it every fix.
    follow: settled && streak === STILL_SETTLE_FIXES,
  };
}

/** Fallback hops must keep pointing within this turn angle to stay a run. */
export const FALLBACK_MAX_TURN_DEG = 100;

/** Same-direction fallback hops required before the run earns metres. */
export const NO_SPEED_FALLBACK_HOPS = 2;

/** A pending bucket must clear this before it is worth a network request. */
export const REPORT_FLOOR_METERS = 0.5;

/** How often the tracker flushes pending buckets while the map is open. */
export const FLUSH_INTERVAL_MS = 5_000;

/** One hex's running total: metres banked and metres awaiting a flush. */
export interface WalkBucket {
  q: number;
  r: number;
  /** The segment-start coordinate attributed to this hex, sent to the server. */
  lat: number;
  lon: number;
  /** Metres awaiting a flush. */
  pending: number;
  /** Metres the server has confirmed it holds. */
  reported: number;
}

export type WalkLedger = Record<string, WalkBucket>;

const key = (q: number, r: number): string => `${q},${r}`;

export function emptyLedger(): WalkLedger {
  return {};
}

/** The chip had something to say about speed at all. */
export function hasSpeed(speed: number | null | undefined): boolean {
  return speed !== null && speed !== undefined && Number.isFinite(speed);
}

/**
 * Whether a fix is walking rather than standing still or driving.
 *
 * Stationary GPS reports a couple of tenths of a metre per second of drift;
 * real walking starts around a metre. The cap matches the server's own
 * plausibility limit, so the client never banks a leg the server will refuse.
 */
export function isMovingFix(speed: number | null | undefined): boolean {
  return (
    hasSpeed(speed) &&
    (speed as number) >= WALK_SPEED_FLOOR_METERS_PER_SECOND &&
    (speed as number) <= MAX_TRACK_SPEED_METERS_PER_SECOND
  );
}

/**
 * The metres one moving step earns: the chip's speed held over the step's
 * duration, capped to the window that was actually measured.
 *
 * `null` when the timing is unusable (missing, negative, or absurdly long) --
 * the caller restarts its phase and credits nothing for this step.
 */
export function paceMeters(speedMps: number, dtSeconds: number): number | null {
  if (!Number.isFinite(speedMps) || speedMps <= 0) {
    return null;
  }
  if (!Number.isFinite(dtSeconds) || dtSeconds <= 0) {
    return null;
  }
  return speedMps * Math.min(dtSeconds, MAX_PACE_STEP_SECONDS);
}

/**
 * The state of the no-speed fallback: how many consecutive same-direction hops
 * have been seen, and which way the last one pointed.
 */
export interface FallbackRunState {
  streak: number;
  prevBearingDeg: number | null;
}

export function emptyFallbackRun(): FallbackRunState {
  return { streak: 0, prevBearingDeg: null };
}

/** The smallest angle between two bearings, 0..180. */
function angularDifference(aDeg: number, bDeg: number): number {
  const raw = Math.abs(aDeg - bDeg) % 360;
  return raw > 180 ? 360 - raw : raw;
}

/**
 * The verdict on one position hop when the chip cannot measure speed.
 *
 * A stationary random walk pokes out a few metres in one hop and back in the
 * next, so a single long hop proves nothing. A hop only counts once two in a
 * row continue in the same direction -- the start of real, purposeful motion --
 * and every subsequent same-direction hop counts until the run breaks.
 */
export function fallbackMoving(
  state: FallbackRunState | null,
  from: { lat: number; lon: number },
  to: { lat: number; lon: number },
): { moving: boolean; meters: number; nextState: FallbackRunState } {
  const delta = haversineMeters([from.lon, from.lat], [to.lon, to.lat]);
  if (!Number.isFinite(delta) || delta < MIN_TRACK_DELTA_METERS) {
    return { moving: false, meters: 0, nextState: emptyFallbackRun() };
  }
  const bearing = bearingDegrees([from.lon, from.lat], [to.lon, to.lat]);
  if (
    state !== null &&
    state.prevBearingDeg !== null &&
    angularDifference(bearing, state.prevBearingDeg) > FALLBACK_MAX_TURN_DEG
  ) {
    // Doubling back is the fingerprint of jitter: a fresh hop that starts a
    // new run rather than continuing a banked one.
    return { moving: false, meters: 0, nextState: { streak: 1, prevBearingDeg: bearing } };
  }
  const streak = (state?.streak ?? 0) + 1;
  const moving = streak >= NO_SPEED_FALLBACK_HOPS;
  return { moving, meters: moving ? delta : 0, nextState: { streak, prevBearingDeg: bearing } };
}

/**
 * Credit earned metres to the hex that contained the walk's start.
 *
 * Returns the unchanged ledger when the step earned nothing, and the ledger
 * with `pending` grown otherwise. The return is a fresh ledger object either
 * way (never a mutation), so callers can diff it for renders.
 */
export function observeSegment(
  ledger: WalkLedger,
  from: { lat: number; lon: number },
  meters: number,
  hexSizeMeters: number,
): WalkLedger {
  if (!Number.isFinite(meters) || meters <= 0) {
    return ledger;
  }
  const { q, r } = lonlatToAxial(from.lon, from.lat, hexSizeMeters);
  const existing = ledger[key(q, r)];
  return {
    ...ledger,
    [key(q, r)]: {
      q,
      r,
      // Keep the first segment's start: it is the reference coordinate
      // attributed to this hex for the whole bucket's life.
      lat: existing !== undefined ? existing.lat : from.lat,
      lon: existing !== undefined ? existing.lon : from.lon,
      pending: (existing?.pending ?? 0) + meters,
      reported: existing?.reported ?? 0,
    },
  };
}

/** Pending metres in one hex, for a display that blends it with the server's. */
export function pendingFor(ledger: WalkLedger, q: number, r: number): number {
  return ledger[key(q, r)]?.pending ?? 0;
}

/** Metres the server already confirmed for one hex. */
export function reportedFor(ledger: WalkLedger, q: number, r: number): number {
  return ledger[key(q, r)]?.reported ?? 0;
}

/** The pending buckets worth sending, rounded to one decimal place. */
export function toReports(
  ledger: WalkLedger,
): { q: number; r: number; lat: number; lon: number; meters: number }[] {
  return Object.values(ledger)
    .filter((bucket) => bucket.pending >= REPORT_FLOOR_METERS)
    .map((bucket) => ({
      q: bucket.q,
      r: bucket.r,
      lat: bucket.lat,
      lon: bucket.lon,
      meters: Math.round(bucket.pending * 10) / 10,
    }));
}

/**
 * Move a successfully reported bucket's pending metres into `reported`.
 *
 * Only called for the buckets whose reports the server accepted, which is why
 * the metres being forgotten here are the ones it has now confirmed it holds.
 * The amount archived is the *rounded* total that was actually sent and
 * accepted, so `pending + reported` stays identical to what the server banked
 * rather than growing a few millimetres a boundary the wire format cannot
 * carry.
 */
export function commitReports(
  ledger: WalkLedger,
  accepted: { q: number; r: number }[],
): WalkLedger {
  if (accepted.length === 0) {
    return ledger;
  }
  let next = ledger;
  for (const { q, r } of accepted) {
    const existing = next[key(q, r)];
    if (existing === undefined || existing.pending === 0) {
      continue;
    }
    const banked = Math.round(existing.pending * 10) / 10;
    next = {
      ...next,
      [key(q, r)]: {
        ...existing,
        pending: 0,
        reported: existing.reported + banked,
      },
    };
  }
  return next;
}