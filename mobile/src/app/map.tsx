/**
 * The map screen: the app's home and its only real screen so far.
 *
 * Holds the viewport, because the viewport is what everything else is derived
 * from -- the tiles on screen, the tile a tap refers to, and the position the
 * claim button uses. Keeping it in one place is what stops the map showing one
 * place and the claim charging for another.
 *
 * The layout is one hemisphere of chrome and one of control, the way a
 * navigation app works: a compact title bar holds identity and mana, the map
 * owns the middle of the screen, and a floating deck at the bottom carries
 * everything the player does with the ground -- the walk-gate progress, the
 * claim action, and step reporting. One primary action per deck, so the thing
 * to press is never a judgement call.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';
import { useRouter } from 'expo-router';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { useSession } from '../auth/session';
import { useClaim, useClaimByLocation, useProfile, useWorld, type Viewport } from '../state/world';
import { usePlayerLocation } from '../location/usePlayerLocation';
import { useWalkTracker } from '../walk/useWalkTracker';
import {
  hexRingAt,
  includePoint,
  lonlatToAxial,
  worldToLayers,
  type Axial,
  type LonLat,
  type OwnershipLayers,
} from '../map/geojson';
import { GoogleMapsMap, type MapHandle } from '../map/MapView';
import { ApiError } from '../api/errors';
import { colours, radius, space, type } from '../ui/theme';

/**
 * Bengaluru: the pre-GPS fallback. The real start position is wherever the
 * player is standing, and this only matters for the split second before the
 * first location fix resolves (or forever, if the fix is refused).
 */
const START: Viewport = { lat: 12.9716, lon: 77.5946, rings: 3 };

/** Before the first world fetch there is no server hex size to project with. */
const HEX_SIZE_FALLBACK = 40;

/** How long a claim or walk notice stays before it removes itself. */
const NOTICE_DURATION_MS = 4_000;

export default function MapScreen() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const [viewport, setViewport] = useState<Viewport>(START);
  const [notice, setNotice] = useState<Notice | null>(null);

  const status = useSession((state) => state.status);

  const world = useWorld(status === 'signed-in' ? viewport : null);
  const profile = useProfile();
  const claim = useClaim();
  const claimHere = useClaimByLocation();
  const place = usePlayerLocation();
  const hexSize = world.data?.hex_size_meters ?? HEX_SIZE_FALLBACK;
  // `refresh` is memoised (useCallback with an empty dep list), so aliasing it
  // gives the mount effect a stable dependency. The `place` object itself is
  // rebuilt every render and listing it would re-arm the effect each frame.
  const refresh = place.refresh;
  const recenter = place.recenter;

  const walk = useWalkTracker({
    latitude: place.latitude,
    longitude: place.longitude,
    speed: place.speed,
    timestamp: place.timestamp,
    enabled: status === 'signed-in',
    hexSizeMeters: hexSize,
  });

  // The hex the player is standing in, and the two numbers that decide whether
  // they may claim it. The server's `/world` total plus the metres still
  // pending in the tracker's buffer is the honest live reading: it cannot show
  // less than the map knows, and it cannot show more than the server will
  // accept, because the server vetoes at claim time regardless.
  const currentHex: Axial | null =
    place.ready && place.latitude !== null && place.longitude !== null
      ? lonlatToAxial(place.longitude, place.latitude, hexSize)
      : null;

  const currentHexRing = useMemo(
    () => (currentHex === null ? null : hexRingAt(currentHex.q, currentHex.r, hexSize)),
    [currentHex, hexSize],
  );

  const claimRequired = world.data?.claim_required_meters ?? 0;
  const walkedHere =
    (world.data?.tiles.find(
      (tile) => currentHex !== null && tile.q === currentHex.q && tile.r === currentHex.r,
    )?.walk_meters ?? 0) +
    (currentHex === null ? 0 : walk.pendingFor(currentHex.q, currentHex.r));
  const remainingHere = Math.max(0, claimRequired - walkedHere);
  const claimableHere = claimRequired <= 0 || walkedHere >= claimRequired;

  const mapRef = useRef<MapHandle>(null);
  // Whether the camera already owed the player its follow-up fit and paid it.
  // A ref, deliberately: it is both latched and read only inside the effect
  // below, never during render, where the ref rules forbid it.
  const fittedRef = useRef(false);
  // The viewport latch in the recentre adjustment must be state (the ref
  // alternative is forbidden during render), and it keeps the adjustment
  // one-shot.
  const [recentred, setRecentred] = useState(false);
  // Any pan/zoom gesture means "stop steering me". A gesture already re-renders
  // (the gesture is how viewport updates), so state costs nothing extra here.
  const [hasPanned, setHasPanned] = useState(false);

  // Ask for a fix as soon as the screen mounts. A player with a warm cached
  // fix gets the camera immediately; the rest get it whenever the GPS answers
  // (or never, if the permission is refused -- the deck says so then).
  useEffect(() => {
    void refresh().catch(() => {
      // The refusal is rendered by the deck via place.message; the map itself
      // has nothing further to do.
    });
  }, [refresh]);

  // Fit the camera once the world fetch for the player's viewport lands.
  // react-query keeps serving the previous viewport's data while a new fetch
  // is in flight, so "is fetching" plus the centre matching the requested
  // point is what proves this is the world for the player's ground. No state
  // is written here; latching happens in the ref and the caller is only ever
  // told about gestures.
  //
  // The fit stays armed while the fix or the matching world is missing. GPS
  // is slower than a network round-trip, so the first world fetch usually
  // resolves while the player is still a blank -- giving up then would leave
  // the camera parked on the pre-GPS fallback viewport for the rest of the
  // session, which is exactly the "opens on Bengaluru, not on me" bug.
  useEffect(() => {
    if (fittedRef.current || world.data === undefined || world.isFetching) {
      return;
    }
    const movedAway =
      Math.abs(world.data.centre.lat - viewport.lat) > 0.01 ||
      Math.abs(world.data.centre.lon - viewport.lon) > 0.01;
    if (movedAway) {
      // Still the previous viewport's data. Stay armed for the real fetch.
      return;
    }
    if (place.latitude === null || place.longitude === null) {
      // The fix has not landed (or a refresh reset it mid-flight). Leave the
      // latch alone: when the fix arrives the viewport recentres and this
      // effect gets called again with both halves present.
      return;
    }
    fittedRef.current = true;
    if (!hasPanned) {
      mapRef.current?.fitBounds({
        ...world.data.bounds,
        ...includePoint(world.data.bounds, [place.longitude, place.latitude]),
      });
    }
  }, [
    world.data,
    world.isFetching,
    viewport.lat,
    viewport.lon,
    place.latitude,
    place.longitude,
    hasPanned,
  ]);

  // A notice is a toast: it says its piece and steps aside. No linger button
  // and no forgotten text at the bottom of the screen.
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (notice === null) {
      return;
    }
    if (noticeTimer.current !== null) {
      clearTimeout(noticeTimer.current);
    }
    noticeTimer.current = setTimeout(() => setNotice(null), NOTICE_DURATION_MS);
    return () => {
      if (noticeTimer.current !== null) {
        clearTimeout(noticeTimer.current);
      }
    };
  }, [notice]);

  const layers = useMemo(
    () => (world.data ? worldToLayers(world.data) : null),
    [world.data],
  );

  const onSelectTile = useCallback(
    (q: number, r: number) => {
      setNotice(null);
      claim.mutate(
        { q, r },
        {
          onSuccess: (result) => {
            // A no-op retry is a success, but "you already own that" is not
            // news. Saying so is clearer than a silent map refresh.
            setNotice(
              result.tile.is_new
                ? { text: `Claimed ${result.tile.q},${result.tile.r} for ${result.mana_spent} mana`, tone: 'success' }
                : { text: 'You already own that hex', tone: 'info' },
            );
          },
          onError: (error: unknown) => {
            setNotice({
              text: error instanceof ApiError ? error.userMessage : 'Could not claim that hex',
              tone: 'danger',
            });
          },
        },
      );
    },
    [claim],
  );

  const onClaimHere = useCallback(async () => {
    if (place.latitude === null || place.longitude === null) {
      return;
    }
    setNotice(null);
    // Bank whatever is still pending before the claim is judged, so the metres
    // that justify it arrive before the request that depends on them. Flush
    // never throws: a failure leaves the metres pending and a message on the
    // deck, and the server answers the claim on its own ledger regardless.
    await walk.flush();
    claimHere.mutate(
      { lat: place.latitude, lon: place.longitude },
      {
        onSuccess: (result) => {
          setNotice(
            result.tile.is_new
              ? { text: `Claimed the hex you are standing on for ${result.mana_spent} mana`, tone: 'success' }
              : { text: 'You already own the hex you are standing on', tone: 'info' },
          );
        },
        onError: (error: unknown) => {
          setNotice({
            text: error instanceof ApiError ? error.userMessage : 'Could not claim that hex',
            tone: 'danger',
          });
        },
      },
    );
  }, [claimHere, place.latitude, place.longitude, walk]);

  // Drift is normally held back by the filter, but if the player ever sees the
  // marker parked somewhere it should not be, one tap snaps it to the current
  // GPS fix and steers the camera after it.
  const onRecenter = useCallback(() => {
    const target = recenter();
    if (target === null) {
      return;
    }
    mapRef.current?.moveTo([target.lon, target.lat]);
    setViewport((current) => ({ ...current, lat: target.lat, lon: target.lon }));
    // The tap is an explicit "steer me again", so it counts as the camera
    // being the player's own again.
    setHasPanned(false);
  }, [recenter]);

  // The first fix recentres the board on the player. Done during render, not in
  // an effect, because this is React's documented "adjust state while
  // rendering" case: the very next render is the one the board is wanted on
  // display, and writing viewport here makes the world query follow the fix
  // without a wasted frame. `recentred` gate keeps the adjustment one-shot.
  if (!recentred && !hasPanned && place.status === 'located') {
    const lat = place.latitude;
    const lon = place.longitude;
    if (lat !== null && lon !== null) {
      setRecentred(true);
      setViewport((current) => ({ ...current, lat, lon }));
    }
  }

  if (status === 'signed-out') {
    // The gate in `index` normally prevents this, but a session can end while
    // this screen is mounted -- a 401 mid-session signs the player out -- and
    // without this the screen would keep rendering somebody's territory.
    return (
      <View style={styles.centred}>
        <Text style={styles.body}>Your session ended.</Text>
        <Pressable style={styles.cta} onPress={() => router.replace('/sign-in')}>
          <Text style={styles.ctaLabel}>Sign in again</Text>
        </Pressable>
      </View>
    );
  }

  const user = profile.data ?? null;

  return (
    <View style={styles.root}>
      <Header
        insetsTop={insets.top}
        displayName={user?.display_name ?? null}
        ownedTileCount={user?.owned_tile_count ?? null}
        onOpenProfile={() => router.push('/profile')}
        onOpenLeaderboard={() => router.push('/leaderboard')}
      />

      {world.isPending ? (
        <View style={styles.centred}>
          <ActivityIndicator color={colours.accent} />
          <Text style={styles.subtle}>Loading the world…</Text>
        </View>
      ) : world.isError ? (
        <View style={styles.centred}>
          <Text style={styles.error}>
            {world.error instanceof ApiError
              ? world.error.userMessage
              : 'Could not load the world'}
          </Text>
          <Pressable style={styles.cta} onPress={() => void world.refetch()}>
            <Text style={styles.ctaLabel}>Try again</Text>
          </Pressable>
        </View>
      ) : layers !== null && world.data !== undefined ? (
        <View style={styles.stage}>
          <GoogleMapsMap
            ref={mapRef}
            layers={layers}
            bounds={world.data.bounds}
            playerPosition={
              place.ready && place.latitude !== null && place.longitude !== null
                ? [place.longitude, place.latitude]
                : null
            }
            playerHeading={place.heading}
            playerAccuracy={place.accuracy}
            currentHexRing={currentHexRing}
            onSelectTile={onSelectTile}
            onViewportChange={(centre: LonLat) => {
              setHasPanned(true);
              setViewport((current) => ({ ...current, lat: centre[1], lon: centre[0] }));
            }}
          />

          <NoticeToast notice={notice} />

          <TileLegend layers={layers} />

          {place.ready && place.latitude !== null && place.longitude !== null ? (
            <Pressable
              accessibilityRole="button"
              accessibilityLabel="Recenter on your position"
              style={({ pressed }) => [styles.recenter, pressed && styles.primaryPressed]}
              onPress={onRecenter}
            >
              <Text style={styles.recenterGlyph}>◎</Text>
            </Pressable>
          ) : null}

          <PlayerDeck
            insetsBottom={insets.bottom}
            canClaimHere={place.ready}
            claimableHere={claimableHere}
            claimRemaining={remainingHere}
            claimRequired={claimRequired}
            claimedHere={walkedHere}
            claimingHere={claimHere.isPending}
            locating={place.locating}
            onLocate={() => void place.refresh()}
            onClaimHere={() => void onClaimHere()}
            locationMessage={place.message}
            accuracy={place.accuracy}
            walkReporting={walk.reporting}
            walkError={walk.error}
            clamped={world.data.latitude_was_clamped === true}
          />
        </View>
      ) : null}
    </View>
  );
}

/** A transient claim/walk result, rendered as a toast on the map. */
type Notice = { text: string; tone: 'success' | 'info' | 'danger' };

/**
 * The title bar: identity left, the world right.
 *
 * The avatar is the first letter of the display name on an accent disc -- a
 * placeholder that needs no image asset and no network. It opens the profile
 * (which is where the account actions, sign-out included, now live), and the
 * compact button beside it opens the leaderboard. No mana here: the budget is
 * deliberately invisible, and what it takes to act is explained by the deck,
 * where the walking happens.
 */
function Header({
  insetsTop,
  displayName,
  ownedTileCount,
  onOpenProfile,
  onOpenLeaderboard,
}: {
  insetsTop: number;
  displayName: string | null;
  ownedTileCount: number | null;
  onOpenProfile: () => void;
  onOpenLeaderboard: () => void;
}) {
  return (
    <View style={[styles.header, { paddingTop: insetsTop + space.sm }]}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel="Open your profile"
        onPress={onOpenProfile}
      >
        {displayName !== null ? (
          <View style={styles.avatar}>
            <Text style={styles.avatarGlyph}>{displayName.slice(0, 1).toUpperCase()}</Text>
          </View>
        ) : (
          <View style={styles.avatar}>
            <ActivityIndicator size="small" color={colours.accent} />
          </View>
        )}
      </Pressable>

      <View style={styles.identity}>
        <Text style={styles.name}>{displayName ?? 'Loading…'}</Text>
        <Text style={styles.subtle}>
          {ownedTileCount !== null ? `${ownedTileCount} hex${ownedTileCount === 1 ? '' : 'es'} claimed` : ' '}
        </Text>
      </View>

      <Pressable
        accessibilityRole="button"
        accessibilityLabel="View the leaderboard"
        style={({ pressed }) => [styles.ghost, pressed && styles.ghostPressed]}
        onPress={onOpenLeaderboard}
      >
        <Text style={styles.ghostLabel}>Ranks</Text>
      </Pressable>
    </View>
  );
}

/**
 * The bottom deck: everything a player does with the ground lives here, in one
 * stacked card, so the map stays the screen and the controls stay the controls.
 *
 * Top: the walk-gate progress for the hex the player is standing in -- the one
 * number that decides whether the button below is live. Then the claim action,
 * the only solid CTA on the screen. Only two actions on purpose: walking is
 * the thing that earns, claiming is the thing that spends, and a bar that is
 * already a glance away needs nothing else.
 */
function PlayerDeck({
  insetsBottom,
  canClaimHere,
  claimableHere,
  claimRemaining,
  claimRequired,
  claimedHere,
  claimingHere,
  locating,
  onLocate,
  onClaimHere,
  locationMessage,
  accuracy,
  walkReporting,
  walkError,
  clamped,
}: {
  insetsBottom: number;
  canClaimHere: boolean;
  claimableHere: boolean;
  claimRemaining: number;
  claimRequired: number;
  claimedHere: number;
  claimingHere: boolean;
  locating: boolean;
  onLocate: () => void;
  onClaimHere: () => void;
  locationMessage: string | null;
  accuracy: number | null;
  walkReporting: boolean;
  walkError: string | null;
  clamped: boolean;
}) {
  return (
    <View style={[styles.deck, { paddingBottom: insetsBottom + space.lg }]}>
      {clamped ? (
        <Text style={styles.clamped}>
          That spot is past the edge of the map projection, so the view was moved
          to the closest point it can show.
        </Text>
      ) : null}

      {canClaimHere ? (
        <WalkProgress
          required={claimRequired}
          claimed={claimedHere}
          remaining={claimRemaining}
          claimable={claimableHere}
          accuracy={accuracy}
          reporting={walkReporting}
          error={walkError}
        />
      ) : (
        <View style={styles.card}>
          <View style={styles.deckRow}>
            <Text style={styles.deckTitle}>Position</Text>
            <Text style={styles.deckMeta}>{locationMessage ?? 'Waiting for satellites…'}</Text>
          </View>
        </View>
      )}

      {canClaimHere && claimableHere ? (
        <Pressable
          accessibilityRole="button"
          accessibilityState={{ disabled: claimingHere }}
          disabled={claimingHere}
          style={({ pressed }) => [
            styles.primary,
            claimingHere ? styles.primaryDisabled : null,
            pressed && styles.primaryPressed,
          ]}
          onPress={onClaimHere}
        >
          {claimingHere ? (
            <ActivityIndicator color={colours.background} size="small" />
          ) : (
            <Text style={styles.primaryLabel}>Claim this hex</Text>
          )}
        </Pressable>
      ) : canClaimHere ? (
        <Pressable
          accessibilityRole="button"
          accessibilityState={{ disabled: true }}
          disabled
          style={[styles.primary, styles.primaryMuted]}
        >
          <Text style={styles.primaryLabelMuted}>
            Walk {Math.ceil(claimRemaining)} m more here
          </Text>
        </Pressable>
      ) : (
        <Pressable
          accessibilityRole="button"
          accessibilityState={{ disabled: locating }}
          disabled={locating}
          style={({ pressed }) => [
            styles.primary,
            locating ? styles.primaryDisabled : null,
            pressed && styles.primaryPressed,
          ]}
          onPress={onLocate}
        >
          {locating ? (
            <ActivityIndicator color={colours.background} size="small" />
          ) : (
            <Text style={styles.primaryLabel}>Find me</Text>
          )}
        </Pressable>
      )}

      {walkError !== null ? <Text style={styles.deckError}>{walkError}</Text> : null}
      <Text style={styles.deckHint}>
        Walk to bank metres here and earn mana along the way.
      </Text>
    </View>
  );
}

/**
 * The walk-gate card: metres earned in the hex the player is standing in, as a
 * progress bar against the claim requirement.
 *
 * Server total plus pending-tracking buffer, so the bar moves as you walk,
 * without the 20-second world poll dragging a few metres behind the player's
 * feet and showing "0 m" at the moment they deserve credit.
 */
function WalkProgress({
  required,
  claimed,
  remaining,
  claimable,
  accuracy,
  reporting,
  error,
}: {
  required: number;
  claimed: number;
  remaining: number;
  claimable: boolean;
  accuracy: number | null;
  reporting: boolean;
  error: string | null;
}) {
  const fraction = required <= 0 ? 0 : Math.max(0, Math.min(1, claimed / required));
  return (
    <View style={styles.card}>
      <View style={styles.deckRow}>
        <Text style={styles.deckTitle}>Walking in this tile</Text>
        {claimable ? (
          <View style={styles.chipReady}>
            <Text style={styles.chipReadyLabel}>Ready to claim</Text>
          </View>
        ) : reporting ? (
          <Text style={styles.deckMeta}>Syncing…</Text>
        ) : null}
      </View>

      {required > 0 ? (
        <>
          <View style={styles.track} accessibilityRole="progressbar">
            <View style={[styles.trackFill, { width: `${fraction * 100}%` }]} />
          </View>
          <View style={styles.deckRow}>
            <Text style={styles.deckValue}>
              {Math.floor(claimed)}
              <Text style={styles.deckValueMuted}> / {Math.floor(required)} m</Text>
            </Text>
            {remaining > 0 ? (
              <Text style={styles.deckMeta}>{Math.ceil(remaining)} m to go</Text>
            ) : null}
          </View>
        </>
      ) : (
        <Text style={styles.deckMeta}>No walk distance required here</Text>
      )}

      {accuracy !== null ? (
        <Text style={styles.deckFine}>GPS ±{Math.round(accuracy)} m</Text>
      ) : null}
      {error !== null ? <Text style={styles.deckError}>{error}</Text> : null}
    </View>
  );
}

/** A claim result or walk notice, floating on the map until it dismisses. */
function NoticeToast({ notice }: { notice: Notice | null }) {
  if (notice === null) {
    return null;
  }
  return (
    <View pointerEvents="none" style={styles.toast}>
      <View style={[styles.toastCard, styles[`toast_${notice.tone}`]]}>
        <Text style={styles.toastText}>{notice.text}</Text>
      </View>
    </View>
  );
}

/**
 * The live board legend: what the three tile colours mean, with the count in
 * view for each.
 *
 * Reading a board should not require trusting your eye for a washed-out fill:
 * the legend says outright that pink ground is somebody else's, and the counts
 * make "is there anyone else around here?" a single glance.
 */
function TileLegend({ layers }: { layers: OwnershipLayers }) {
  const rows = [
    { label: 'You', count: layers.own.features.length, colour: colours.own },
    { label: 'Others', count: layers.rival.features.length, colour: colours.rival },
    { label: 'Open', count: layers.unclaimed.features.length, colour: colours.unclaimed },
  ];
  return (
    <View pointerEvents="none" style={styles.legend}>
      {rows.map((row) => (
        <View key={row.label} style={styles.legendItem}>
          <View style={[styles.legendSwatch, { backgroundColor: row.colour }]} />
          <Text style={styles.legendLabel}>
            {row.label} · {row.count}
          </Text>
        </View>
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: colours.background },

  // ---- Title bar ---------------------------------------------------------
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.md,
    paddingHorizontal: space.lg,
    paddingBottom: space.md,
  },
  avatar: {
    width: 40,
    height: 40,
    borderRadius: 20,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: colours.ownMuted,
    borderWidth: 1,
    borderColor: colours.border,
  },
  avatarGlyph: { ...type.heading, color: colours.accent },
  identity: { flex: 1 },
  name: { ...type.heading, color: colours.text },
  subtle: { ...type.caption, color: colours.textMuted },
  ghost: {
    paddingHorizontal: space.sm,
    paddingVertical: space.sm,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colours.border,
  },
  ghostPressed: { backgroundColor: colours.surface },
  ghostLabel: { ...type.caption, color: colours.textMuted },

  // ---- Map -----------------------------------------------------------------
  stage: { flex: 1, position: 'relative' },

  // A result toast floats centred at the top of the map, clear of both the
  // header and the deck.
  toast: {
    position: 'absolute',
    top: space.md,
    left: space.lg,
    right: space.lg,
    alignItems: 'center',
  },
  toastCard: {
    borderRadius: radius.md,
    borderWidth: 1,
    paddingHorizontal: space.lg,
    paddingVertical: space.md,
    backgroundColor: colours.surfaceRaised,
    shadowColor: '#000',
    shadowOpacity: 0.3,
    shadowRadius: 12,
    elevation: 8,
  },
  toast_success: { borderColor: colours.success },
  toast_info: { borderColor: colours.accent },
  toast_danger: { borderColor: colours.danger },
  toastText: { ...type.body, color: colours.text, textAlign: 'center' },

  // A small crosshair over the map's right edge: jump the marker to the live
  // GPS reading (and bring the camera along) when drift has parked it somewhere
  // it should not be. Sits clear of the header above and the deck below, and of
  // the top-centre toast.
  recenter: {
    position: 'absolute',
    top: space.xl,
    right: space.md,
    width: 44,
    height: 44,
    borderRadius: 22,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: colours.surfaceRaised,
    borderWidth: 1,
    borderColor: colours.border,
    shadowColor: '#000',
    shadowOpacity: 0.25,
    shadowRadius: 8,
    shadowOffset: { width: 0, height: 2 },
    elevation: 6,
  },
  recenterGlyph: { ...type.bodyStrong, color: colours.accent },

  // The board legend: three swatches with live counts, parked clear of the
  // header above and the deck below, mirroring the recentre button's elevation.
  legend: {
    position: 'absolute',
    top: space.xl,
    left: space.md,
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.md,
    backgroundColor: colours.surfaceRaised,
    borderRadius: radius.pill,
    borderWidth: 1,
    borderColor: colours.border,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
    shadowColor: '#000',
    shadowOpacity: 0.25,
    shadowRadius: 8,
    shadowOffset: { width: 0, height: 2 },
    elevation: 6,
  },
  legendItem: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
  },
  legendSwatch: {
    width: 12,
    height: 12,
    borderRadius: 3,
  },
  legendLabel: { ...type.caption, color: colours.text },

  // ---- Bottom deck ---------------------------------------------------------
  deck: {
    position: 'absolute',
    left: space.lg,
    right: space.lg,
    bottom: 0,
    gap: space.md,
  },
  card: {
    backgroundColor: colours.surfaceRaised,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colours.border,
    padding: space.lg,
    gap: space.md,
  },
  deckRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: space.md,
  },
  deckTitle: { ...type.label, color: colours.text },
  deckMeta: { ...type.label, color: colours.textMuted, flexShrink: 1 },
  deckValue: { ...type.numeric, color: colours.text },
  deckValueMuted: { ...type.numeric, color: colours.textFaint },
  deckFine: { ...type.caption, color: colours.textFaint },
  deckHint: { ...type.caption, color: colours.textFaint, textAlign: 'center' },
  deckError: { ...type.caption, color: colours.danger, textAlign: 'center' },
  track: {
    height: 6,
    borderRadius: radius.pill,
    backgroundColor: colours.surface,
    overflow: 'hidden',
  },
  trackFill: { height: '100%', borderRadius: radius.pill, backgroundColor: colours.accent },
  chipReady: {
    backgroundColor: colours.ownMuted,
    borderRadius: radius.pill,
    paddingHorizontal: space.sm,
    paddingVertical: 2,
  },
  chipReadyLabel: { ...type.caption, color: colours.accent },

  // The one solid thing on the screen: the claim (or locate) action.
  primary: {
    minHeight: 52,
    borderRadius: radius.lg,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: colours.accent,
    paddingHorizontal: space.lg,
  },
  primaryPressed: { opacity: 0.85 },
  primaryDisabled: { opacity: 0.5 },
  primaryMuted: { backgroundColor: colours.surface, borderWidth: 1, borderColor: colours.border },
  primaryLabel: { ...type.bodyStrong, color: colours.background },
  primaryLabelMuted: { ...type.bodyStrong, color: colours.textMuted },

  clamped: {
    ...type.caption,
    color: colours.warning,
    textAlign: 'center',
    backgroundColor: colours.surface,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colours.border,
    padding: space.sm,
  },

  // ---- Full-bleed states (loading / error / signed out) --------------------
  centred: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    gap: space.md,
    padding: space.xl,
  },
  body: { ...type.body, color: colours.text },
  error: { ...type.body, color: colours.danger, textAlign: 'center' },
  cta: {
    backgroundColor: colours.accent,
    borderRadius: radius.md,
    paddingHorizontal: space.lg,
    paddingVertical: space.md,
  },
  ctaLabel: { ...type.bodyStrong, color: colours.background },
});