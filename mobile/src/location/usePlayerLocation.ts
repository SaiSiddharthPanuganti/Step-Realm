import { useCallback, useEffect, useRef, useState } from 'react';
import * as Location from 'expo-location';

import { bearingDegrees, haversineMeters } from '../map/geojson';
import { driftFilter, emptyStillness, type Stillness } from '../walk/accumulate';

/**
 * Where the player is, continuously enough for the walk tracker and marker.
 *
 * A watch rather than a single fix, because walking is the point of the map
 * screen now: the hex a player is building progress in, and the direction their
 * marker points, both need the device's position to keep arriving. The earlier
 * single-fix design was a battery decision that the walk-gating feature made
 * obsolete -- there is nothing to gain from a fix that stops updating the
 * moment it is taken.
 *
 * Accuracy is `High`, not `Balanced`, for the same reason: a 2 m resolution
 * matters when 20 m inside a 40 m hex is the difference between claimable and
 * "keep walking". The power cost is real and is accepted, deliberately, because
 * the watch only runs while the map screen is mounted.
 *
 * Heading prefers the chip's compass reading, falling back to the bearing
 * between consecutive fixes -- cheap GPS reports a heading only while moving,
 * and the fallback keeps the marker honest when it does not.
 *
 * One fix in the door does not automatically move the marker. A standing phone
 * rocks its fix point several metres, which would flick the marker and the
 * "which tile am I in" ring across hexes while the player stays put. Every fix
 * passes through `driftFilter` (in `src/walk/accumulate`): the chip's speed is
 * the arbiter of movement, a still spell holds the position and then settles
 * once, and the marker re-anchors only on a genuine escape. The heading and the
 * walk tracker then see a clean stream -- the marker holds still when the
 * player is, and moves when they do.
 */

export type LocationStatus =
  | 'idle'
  | 'requesting'
  /** A fix is being delivered, and will keep arriving while mounted. */
  | 'located'
  /** The player declined. */
  | 'denied'
  /** The device has location services switched off entirely. */
  | 'disabled'
  | 'error';

interface LocationState {
  status: LocationStatus;
  latitude: number | null;
  longitude: number | null;
  /** Degrees clockwise from north, from the chip or derived from the walk. */
  heading: number | null;
  /** Metres per second, when the chip reports one. */
  speed: number | null;
  /** Epoch milliseconds of the most recent fix. */
  timestamp: number | null;
  /** Radius of the fix's uncertainty in metres, when known. */
  accuracy: number | null;
  message: string | null;
}

export interface PlayerLocation extends LocationState {
  /** (Re)start the permission flow and a fresh fix. Stable across renders. */
  refresh: () => Promise<void>;
  /**
   * Snap the marker to the player's *current* raw fix, filter and still spell
   * notwithstanding. Drift is normally held back (see the module comment), but
   * when the player sees the marker somewhere it should not be, one tap gives
   * them the truth. Returns the coordinates the marker jumped to, so the map
   * can move the camera along, or null if no fix has ever landed. Stable.
   */
  recenter: () => { lat: number; lon: number } | null;
  /** True until the first fix of a refresh has arrived. */
  locating: boolean;
  /** Whether a claim-here action can be attempted at all. */
  ready: boolean;
}

const INITIAL: LocationState = {
  status: 'idle',
  latitude: null,
  longitude: null,
  heading: null,
  speed: null,
  timestamp: null,
  accuracy: null,
  message: null,
};

/** Accuracy and cadence for the walking watch. */
const WATCH_OPTIONS: Location.LocationOptions = {
  accuracy: Location.Accuracy.High,
  timeInterval: 1000,
  distanceInterval: 3,
};

/** Ignore heading/bearing wobble from a standing start. */
const STANDING_MOVEMENT_METERS = 1;

export function usePlayerLocation(): PlayerLocation {
  const [state, setState] = useState<LocationState>(INITIAL);
  // Guards the async continuations below: a fix that arrives after the screen
  // is gone must not set state, which warns and leaks under React 19.
  const mounted = useRef(true);
  // The long-lived location subscription, so `refresh` can restart it without
  // piling up duplicate watches and so unmount can tear it down.
  const watchRef = useRef<{ remove: () => void } | null>(null);
  // The previous fix, kept for the heading fallback: a fix that cannot tell us
  // which way the player faces can still tell us which way they moved.
  const previousRef = useRef<{ latitude: number; longitude: number } | null>(null);
  // The position the player is actually shown at, held through still spells so
  // a standing phone's fix-point wander never drags the marker or the hex ring
  // across the map (see `driftFilter`). `{lat, lon}` to match the accumulator.
  const releasedRef = useRef<{ lat: number; lon: number } | null>(null);
  const stillnessRef = useRef<Stillness>(emptyStillness());
  // The rawest fix the chip has reported, kept separate from `releasedRef` so a
  // recenter tap can jump the marker to where GPS actually is, not where the
  // drift filter last decided it should be shown.
  const latestFixRef = useRef<{ lat: number; lon: number } | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      watchRef.current?.remove();
      watchRef.current = null;
    };
  }, []);

  const applyFix = useCallback((fix: Location.LocationObject): void => {
    const { latitude, longitude, heading, speed, accuracy } = fix.coords;
  // The fix's epoch lives on the LocationObject, not the coords, in this
  // expo-location version.
  const { timestamp } = fix;
    latestFixRef.current = { lat: latitude, lon: longitude };
    const previous = previousRef.current;
    const verdict = driftFilter({
      speed,
      stillness: stillnessRef.current,
      fix: { lat: latitude, lon: longitude },
      released: releasedRef.current,
    });
    releasedRef.current = verdict.released;
    stillnessRef.current = verdict.stillness;
    // Heading is only honest while the marker is actually moving: on a still
    // spell the arrow gets nothing to point along, so it is hidden rather than
    // left spinning on the fix-point wander.
    let effectiveHeading: number | null = null;
    if (verdict.follow) {
      effectiveHeading =
        heading !== null && heading !== undefined && Number.isFinite(heading) && heading !== 0
          ? heading
          : null;
      if (effectiveHeading === null && previous !== null) {
        const moved = haversineMeters(
          [previous.longitude, previous.latitude],
          [longitude, latitude],
        );
        if (moved > STANDING_MOVEMENT_METERS) {
          effectiveHeading = bearingDegrees(
            [previous.longitude, previous.latitude],
            [longitude, latitude],
          );
        }
      }
    }
    previousRef.current = { latitude, longitude };
    setState({
      status: 'located',
      latitude: verdict.released.lat,
      longitude: verdict.released.lon,
      heading: effectiveHeading,
      speed: speed !== null && speed !== undefined && Number.isFinite(speed) ? speed : null,
      timestamp:
        timestamp !== null && timestamp !== undefined && Number.isFinite(timestamp)
          ? timestamp
          : null,
      accuracy:
        accuracy !== null && accuracy !== undefined && Number.isFinite(accuracy)
          ? accuracy
          : null,
      message: null,
    });
  }, []);

  // See `PlayerLocation.recenter`: jump the shown position to the rawest fix
  // the chip has delivered, clear the still spell so the hold resumes from
  // here rather than from the drifted anchor, and re-render this instant so
  // the marker moves without waiting for the next second's tick.
  const recenter = useCallback((): { lat: number; lon: number } | null => {
    const fix = latestFixRef.current;
    if (fix === null) {
      return null;
    }
    releasedRef.current = fix;
    stillnessRef.current = emptyStillness();
    previousRef.current = { latitude: fix.lat, longitude: fix.lon };
    setState((current) => ({
      ...current,
      status: 'located',
      latitude: fix.lat,
      longitude: fix.lon,
      heading: null,
    }));
    return fix;
  }, []);

  const startWatch = useCallback(async (): Promise<void> => {
    if (watchRef.current !== null) {
      // Already subscribed; a later refresh only needs a fresh immediate fix.
      return;
    }
    const subscription = await Location.watchPositionAsync(WATCH_OPTIONS, applyFix);
    if (!mounted.current) {
      subscription.remove();
      return;
    }
    watchRef.current = subscription;
  }, [applyFix]);

  const refresh = useCallback(async (): Promise<void> => {
    setState({
      status: 'requesting',
      latitude: null,
      longitude: null,
      heading: null,
      speed: null,
      timestamp: null,
      accuracy: null,
      message: null,
    });

    try {
      if (!(await Location.hasServicesEnabledAsync())) {
        // Distinct from a denial: no permission prompt can fix this, the player
        // has to change a system setting, so the message has to say which.
        setState({
          status: 'disabled',
          latitude: null,
          longitude: null,
          heading: null,
speed: null,
      timestamp: null,
      accuracy: null,
      message: 'Turn on location services to claim the ground you are standing on.',
        });
        return;
      }

      const permission = await Location.requestForegroundPermissionsAsync();
      if (!permission.granted) {
        setState({
          status: 'denied',
          latitude: null,
          longitude: null,
          heading: null,
speed: null,
      timestamp: null,
      accuracy: null,
      message: 'StepRealm needs location to claim the hex you are standing on.',
        });
        return;
      }

      const fix = await Location.getCurrentPositionAsync({
        accuracy: Location.Accuracy.High,
      });
      if (mounted.current) {
        applyFix(fix);
      }
      // The watch carries on from here; a failure to start it must not undo the
      // fix the player already has.
      await startWatch().catch(() => {
        // The watch may fail when the platform refuses a second stream; the
        // current fix is still valid and the walk tracker works with one-shot
        // refreshes from the locate button.
      });
    } catch (cause) {
      if (!mounted.current) {
        return;
      }
      setState({
        status: 'error',
        latitude: null,
        longitude: null,
        heading: null,
        speed: null,
        timestamp: null,
        accuracy: null,
        message: cause instanceof Error ? cause.message : 'Could not get a location fix',
      });
    }
  }, [applyFix, startWatch]);

  return {
    ...state,
    refresh,
    recenter,
    // Derived rather than stored, so a spinner can never be left on by a branch
    // that returns early.
    locating: state.status === 'requesting',
    ready: state.status === 'located',
  };
}