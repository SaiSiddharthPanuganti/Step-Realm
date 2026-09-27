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
import { useClaim, useProfile, useWorld, type Viewport } from '../state/world';
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
