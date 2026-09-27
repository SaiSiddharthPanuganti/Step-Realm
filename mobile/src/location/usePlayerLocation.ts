import { useCallback, useEffect, useRef, useState } from 'react';
import * as Location from 'expo-location';

/**
 * Where the player is, for the "claim this hex" button.
 *
 * A single foreground fix rather than a continuous watch, on purpose. Claiming
 * is a deliberate act at a moment the player chose, so there is nothing to gain
 * from a subscription that runs for as long as the screen is open, and a real
 * cost: a continuous GPS watch is one of the fastest ways to drain a battery, and
 * the map only needs the player to be roughly where they tapped.
 *
 * Accuracy is `Balanced` rather than `High` for the same reason. A hex is 40 m
 * across, so a fix accurate to a few tens of metres is enough to pick the right
 * tile, and `High` spends noticeably more power to resolve a difference the
 * server cannot act on -- it snaps the coordinate to the hex centre regardless.
 */

export type LocationStatus =
  | 'idle'
  | 'requesting'
  /** A fix, good enough to pick a hex. */
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
  message: string | null;
}

export interface PlayerLocation extends LocationState {
  /** Take a fresh fix. Stable across renders, so it is safe in a dep array. */
  refresh: () => Promise<void>;
  /** True only while a request is in flight. */
  locating: boolean;
  /** Whether a claim-here action can be attempted at all. */
  ready: boolean;
}

const INITIAL: LocationState = {
  status: 'idle',
  latitude: null,
  longitude: null,
  message: null,
};

export function usePlayerLocation(): PlayerLocation {
  const [state, setState] = useState<LocationState>(INITIAL);
  // Guards the async continuations below: a fix that arrives after the screen is
  // gone must not set state, which warns and leaks under React 19.
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const refresh = useCallback(async (): Promise<void> => {
    setState({ status: 'requesting', latitude: null, longitude: null, message: null });

    try {
      if (!(await Location.hasServicesEnabledAsync())) {
        // Distinct from a denial: no permission prompt can fix this, the player
        // has to change a system setting, so the message has to say which.
        setState({
          status: 'disabled',
          latitude: null,
          longitude: null,
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
          message: 'StepRealm needs location to claim the hex you are standing on.',
        });
        return;
      }

      const fix = await Location.getCurrentPositionAsync({
        accuracy: Location.Accuracy.Balanced,
      });

      if (!mounted.current) {
        return;
      }
      setState({
        status: 'located',
        latitude: fix.coords.latitude,
        longitude: fix.coords.longitude,
        message: null,
      });
    } catch (cause) {
      if (!mounted.current) {
        return;
      }
      setState({
        status: 'error',
        latitude: null,
        longitude: null,
        message: cause instanceof Error ? cause.message : 'Could not get a location fix',
      });
    }
  }, []);

  return {
    ...state,
    refresh,
    // Derived rather than stored, so a spinner can never be left on by a branch
    // that returns early.
    locating: state.status === 'requesting',
    ready: state.status === 'located',
  };
}
