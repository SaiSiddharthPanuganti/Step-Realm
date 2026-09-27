/**
 * The map screen: the app's home and its only real screen so far.
 *
 * Holds the viewport, because the viewport is what everything else is derived
 * from -- the tiles on screen, the tile a tap refers to, and the position the
 * claim button uses. Keeping it in one place is what stops the map showing one
 * place and the claim charging for another.
 */

import { useCallback, useMemo, useState } from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';
import { useRouter } from 'expo-router';

import { useSession } from '../auth/session';
import { useClaim, useClaimByLocation, useProfile, useWorld, type Viewport } from '../state/world';
import { useStepTracker, type StepTrackerStatus } from '../steps/useStepTracker';
import { usePlayerLocation } from '../location/usePlayerLocation';
import { manaWorth } from '../steps/checkpoint';
import { worldToLayers, type LonLat } from '../map/geojson';
import { PlaceholderMap } from '../map/MapView';
import { ApiError } from '../api/errors';
import { colours, radius, space, type } from '../ui/theme';

/** Bengaluru. The world origin, and close enough to the default leash. */
const START: Viewport = { lat: 12.9716, lon: 77.5946, rings: 3 };

export default function MapScreen() {
  const router = useRouter();
  const [viewport, setViewport] = useState<Viewport>(START);
  const [notice, setNotice] = useState<string | null>(null);

  const status = useSession((state) => state.status);
  const signOut = useSession((state) => state.signOut);

  const world = useWorld(status === 'signed-in' ? viewport : null);
  const profile = useProfile();
  const claim = useClaim();
  const claimHere = useClaimByLocation();
  const steps = useStepTracker();
  const place = usePlayerLocation();

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
                ? `Claimed ${result.tile.q},${result.tile.r} for ${result.mana_spent} mana`
                : 'You already own that hex',
            );
          },
          onError: (error: unknown) => {
            setNotice(
              error instanceof ApiError
                ? error.userMessage
                : 'Could not claim that hex',
            );
          },
        },
      );
    },
    [claim],
  );

  const onClaimHere = useCallback(() => {
    if (place.latitude === null || place.longitude === null) {
      return;
    }
    setNotice(null);
    claimHere.mutate(
      { lat: place.latitude, lon: place.longitude },
      {
        onSuccess: (result) => {
          setNotice(
            result.tile.is_new
              ? `Claimed the hex you are standing on for ${result.mana_spent} mana`
              : 'You already own the hex you are standing on',
          );
        },
        onError: (error: unknown) => {
          setNotice(
            error instanceof ApiError ? error.userMessage : 'Could not claim that hex',
          );
        },
      },
    );
  }, [claimHere, place.latitude, place.longitude]);

  const onSyncSteps = useCallback(() => {
    setNotice(null);
    void steps.sync().catch(() => {
      // The tracker keeps the steps pending, and its own error is rendered
      // below, so this only has to stop the rejection escaping as an unhandled
      // promise.
    });
  }, [steps]);

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
      <View style={styles.header}>
        <View style={styles.headerText}>
          <Text style={styles.name}>{user?.display_name ?? 'Loading…'}</Text>
          <Text style={styles.subtle}>
            {user ? `${user.owned_tile_count} hexes claimed` : ' '}
          </Text>
        </View>

        <ManaPill mana={user?.mana ?? 0} cap={user?.mana_max ?? 0} />

        <Pressable
          accessibilityRole="button"
          onPress={() => {
            void signOut();
            router.replace('/sign-in');
          }}
        >
          <Text style={styles.subtle}>Sign out</Text>
        </Pressable>
      </View>

      <ActionBar
        pendingSteps={steps.pendingSteps}
        stepStatus={steps.status}
        syncing={steps.syncing}
        onSyncSteps={onSyncSteps}
        mana={user?.mana ?? 0}
        canClaimHere={place.ready}
        claimingHere={claimHere.isPending}
        locating={place.locating}
        onLocate={() => void place.refresh()}
        onClaimHere={onClaimHere}
        locationMessage={place.message}
        stepError={steps.error}
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
        <PlaceholderMap
          layers={layers}
          bounds={world.data.bounds}
          playerPosition={null}
          onSelectTile={onSelectTile}
          onViewportChange={(centre: LonLat) =>
            setViewport((current) => ({ ...current, lat: centre[1], lon: centre[0] }))
          }
        />
      ) : null}

      {notice !== null ? (
        <View style={styles.notice}>
          <Text style={styles.noticeText}>{notice}</Text>
        </View>
      ) : null}

      {claim.isPending ? (
        <View style={styles.pending}>
          <ActivityIndicator color={colours.accent} size="small" />
        </View>
      ) : null}

      {world.data?.latitude_was_clamped === true ? (
        <Text style={styles.clamped}>
          That spot is past the edge of the map projection, so the view was moved
          to the closest point it can show.
        </Text>
      ) : null}
    </View>
  );
}

/**
 * The two things a player does between looking at the map: report the walk, and
 * take the ground.
 *
 * The step count is shown next to the mana it is actually worth, not the mana it
 * might be worth when rounded up. 1,500 steps is worth 0 mana on its own because
 * the server carries the half block forward, and a button promising 20 that
 * credits nothing is worse than one promising nothing.
 */
function ActionBar({
  pendingSteps,
  stepStatus,
  syncing,
  onSyncSteps,
  mana,
  canClaimHere,
  claimingHere,
  locating,
  onLocate,
  onClaimHere,
  locationMessage,
  stepError,
}: {
  pendingSteps: number;
  stepStatus: StepTrackerStatus;
  syncing: boolean;
  onSyncSteps: () => void;
  mana: number;
  canClaimHere: boolean;
  claimingHere: boolean;
  locating: boolean;
  onLocate: () => void;
  onClaimHere: () => void;
  locationMessage: string | null;
  stepError: string | null;
}) {
  const worth = manaWorth(pendingSteps);
  const hasSteps = pendingSteps > 0;

  return (
    <View style={styles.actionBar}>
      <Pressable
        accessibilityRole="button"
        accessibilityState={{ disabled: !hasSteps || syncing }}
        disabled={!hasSteps || syncing}
        style={[styles.action, (!hasSteps || syncing) && styles.actionDisabled]}
        onPress={onSyncSteps}
      >
        {syncing ? (
          <ActivityIndicator color={colours.background} size="small" />
        ) : (
          <Text style={styles.actionLabel}>
            {hasSteps
              ? `Report ${pendingSteps} steps · +${worth} mana`
              : stepLabel(stepStatus)}
          </Text>
        )}
      </Pressable>

      {canClaimHere ? (
        <Pressable
          accessibilityRole="button"
          accessibilityState={{ disabled: claimingHere }}
          disabled={claimingHere}
          style={[styles.action, claimingHere && styles.actionDisabled]}
          onPress={onClaimHere}
        >
          {claimingHere ? (
            <ActivityIndicator color={colours.background} size="small" />
          ) : (
            <Text style={styles.actionLabel}>Claim this hex · {mana} mana</Text>
          )}
        </Pressable>
      ) : (
        <Pressable
          accessibilityRole="button"
          accessibilityState={{ disabled: locating }}
          disabled={locating}
          style={[styles.action, styles.actionSecondary, locating && styles.actionDisabled]}
          onPress={onLocate}
        >
          {locating ? (
            <ActivityIndicator color={colours.accent} size="small" />
          ) : (
            <Text style={styles.actionLabelSecondary}>
              {locationMessage ?? 'Find me to claim this hex'}
            </Text>
          )}
        </Pressable>
      )}

      {stepError !== null ? <Text style={styles.actionError}>{stepError}</Text> : null}
    </View>
  );
}

/** Why there are no steps to report, which is usually not a fault. */
function stepLabel(status: StepTrackerStatus): string {
  switch (status) {
    case 'loading':
      return 'Reading your step history…';
    case 'unavailable':
      return 'No step sensor on this device';
    case 'denied':
      return 'Step counting is turned off';
    default:
      return 'Walk to earn mana';
  }
}

/**
 * Mana as a bar, not a number.
 *
 * A number alone cannot answer the only question a player has while walking:
 * can I afford the next hex. The bar answers it against the cap, and the cap
 * comes from the server so the two cannot disagree.
 */
function ManaPill({ mana, cap }: { mana: number; cap: number }) {
  const fraction = cap === 0 ? 0 : Math.max(0, Math.min(1, mana / cap));
  return (
    <View style={styles.mana} accessibilityRole="progressbar">
      <View style={styles.manaTrack}>
        <View style={[styles.manaFill, { width: `${fraction * 100}%` }]} />
      </View>
      <Text style={styles.manaLabel}>
        {mana} / {cap}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: colours.background },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.md,
    paddingHorizontal: space.lg,
    paddingTop: space.xl,
    paddingBottom: space.md,
  },
  headerText: { flex: 1 },
  name: { ...type.heading, color: colours.text },
  subtle: { ...type.caption, color: colours.textMuted },
  actionBar: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: space.sm,
    paddingHorizontal: space.lg,
    paddingBottom: space.md,
  },
  actionError: {
    ...type.caption,
    color: colours.danger,
    width: '100%',
  },
  action: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: colours.accent,
    borderRadius: radius.md,
    paddingHorizontal: space.md,
    paddingVertical: space.md,
    minHeight: 44,
  },
  actionSecondary: {
    backgroundColor: 'transparent',
    borderWidth: 1,
    borderColor: colours.border,
  },
  actionDisabled: { opacity: 0.45 },
  actionLabel: { ...type.bodyStrong, color: colours.background, textAlign: 'center' },
  actionLabelSecondary: { ...type.caption, color: colours.textMuted, textAlign: 'center' },
  mana: { alignItems: 'center', gap: space.xs, width: 92 },
  manaTrack: {
    width: '100%',
    height: 6,
    borderRadius: radius.pill,
    backgroundColor: colours.surface,
    overflow: 'hidden',
  },
  manaFill: { height: '100%', backgroundColor: colours.mana },
  manaLabel: { ...type.caption, color: colours.textMuted },
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
  notice: {
    position: 'absolute',
    left: space.lg,
    right: space.lg,
    bottom: space.xl,
    backgroundColor: colours.surfaceRaised,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colours.border,
    padding: space.md,
  },
  noticeText: { ...type.body, color: colours.text },
  pending: {
    position: 'absolute',
    top: space.xl + 60,
    right: space.lg,
  },
  clamped: {
    ...type.caption,
    color: colours.warning,
    paddingHorizontal: space.lg,
    paddingBottom: space.md,
  },
});
