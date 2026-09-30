/**
 * The leaderboard: every player ranked by hexes claimed.
 *
 * Territory, not mana, is the metric -- the cap of 200 means almost every
 * active player sits on a full bar, and a board that never changes order is
 * not a board. The caller's row is pinned above the list so a player who has
 * claimed nothing still sees their standing instead of a sea of strangers.
 */

import { useEffect } from 'react';
import {
  ActivityIndicator,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { useRouter } from 'expo-router';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { useSession } from '../auth/session';
import { useLeaderboard } from '../state/leaderboard';
import type { LeaderboardEntry } from '../api/types';
import { ApiError } from '../api/errors';
import { colours, radius, space, type } from '../ui/theme';

/** One page, matching the server's default page size. */
const PAGE_SIZE = 50;

export default function LeaderboardScreen() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const status = useSession((state) => state.status);
  const sessionUser = useSession((state) => state.user);
  const board = useLeaderboard(PAGE_SIZE);

  // A 401 anywhere signs the player out; a leaderboard with no viewer has
  // nothing to pin to the top.
  useEffect(() => {
    if (status === 'signed-out') {
      router.replace('/sign-in');
    }
  }, [status, router]);

  const viewer = board.data?.viewer ?? null;
  const entries = board.data?.entries ?? [];
  const ownRowAtTop =
    viewer !== null && entries.length > 0 && entries[0].user_id === viewer.user_id;

  return (
    <View style={[styles.root, { paddingTop: insets.top }]}>
      <View style={styles.header}>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Back to the map"
          style={({ pressed }) => [styles.back, pressed && styles.pressed]}
          onPress={() => router.back()}
        >
          <Text style={styles.backGlyph}>‹</Text>
        </Pressable>
        <View style={styles.headerText}>
          <Text style={styles.headerTitle}>Leaderboard</Text>
          <Text style={styles.headerSubtitle}>Ranked by territory claimed</Text>
        </View>
      </View>

      {board.isPending ? (
        <View style={styles.centred}>
          <ActivityIndicator color={colours.accent} />
          <Text style={styles.subtle}>Loading the board…</Text>
        </View>
      ) : board.isError ? (
        <View style={styles.centred}>
          <Text style={styles.error}>
            {board.error instanceof ApiError
              ? board.error.userMessage
              : 'Could not load the leaderboard'}
          </Text>
          <Pressable
            style={({ pressed }) => [styles.cta, pressed && styles.pressed]}
            onPress={() => void board.refetch()}
          >
            <Text style={styles.ctaLabel}>Try again</Text>
          </Pressable>
        </View>
      ) : (
        <ScrollView style={styles.list} contentContainerStyle={styles.content}>
          <View style={styles.listHeader}>
            {viewer !== null && !ownRowAtTop ? (
              <View style={styles.viewerCard}>
                <Text style={styles.viewerName}>{sessionUser?.display_name ?? 'You'}</Text>
                <Text style={styles.viewerRank}>
                  {viewer.rank === null
                    ? 'Claim your first hex to be ranked'
                    : `You are #${viewer.rank} with ${viewer.owned_tile_count} hexes`}
                </Text>
              </View>
            ) : null}
            {board.data?.degraded === true ? (
              <Text style={styles.degraded}>
                Live board unavailable right now — showing a snapshot.
              </Text>
            ) : null}
          </View>

          {entries.map((entry) => (
            <View key={entry.user_id}>
              <PlayerRow entry={entry} highlight={entry.user_id === viewer?.user_id} />
              <View style={styles.separator} />
            </View>
          ))}
        </ScrollView>
      )}
    </View>
  );
}

/**
 * One ranked player. The caller's own row is highlighted so finding yourself
 * in the list is a glance rather than a scan of every name. */
function PlayerRow({ entry, highlight }: { entry: LeaderboardEntry; highlight: boolean }) {
  return (
    <View style={[styles.row, highlight && styles.rowSelf]}>
      <View style={styles.rankCell}>
        <Text style={highlight ? styles.rankSelf : styles.rank}>{entry.rank}</Text>
      </View>
      <View style={styles.rowIdentity}>
        <Text style={styles.rowName} numberOfLines={1}>
          {entry.display_name}
        </Text>
        <Text style={styles.rowMana}>{entry.mana} mana</Text>
      </View>
      <View style={styles.rowScore}>
        <Text style={styles.rowCount}>{entry.owned_tile_count}</Text>
        <Text style={styles.rowCountUnit}>
          {entry.owned_tile_count === 1 ? 'hex' : 'hexes'}
        </Text>
      </View>
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
    paddingBottom: space.md,
  },
  back: {
    width: 36,
    height: 36,
    borderRadius: radius.pill,
    alignItems: 'center',
    justifyContent: 'center',
    borderWidth: 1,
    borderColor: colours.border,
    backgroundColor: colours.surface,
  },
  backGlyph: { ...type.heading, color: colours.text, marginTop: -2 },
  pressed: { opacity: 0.8 },
  headerText: { flex: 1 },
  headerTitle: { ...type.heading, color: colours.text },
  headerSubtitle: { ...type.caption, color: colours.textMuted },

  content: {
    paddingHorizontal: space.lg,
    paddingBottom: space.xxl,
  },
  list: { flex: 1 },
  listHeader: { gap: space.md, marginBottom: space.md },
  viewerCard: {
    backgroundColor: colours.ownMuted,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colours.border,
    padding: space.md,
    gap: 2,
  },
  viewerName: { ...type.heading, color: colours.accent },
  viewerRank: { ...type.caption, color: colours.textMuted },
  degraded: { ...type.caption, color: colours.warning, textAlign: 'center' },

  separator: { height: space.sm },

  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.md,
    backgroundColor: colours.surfaceRaised,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colours.border,
    paddingHorizontal: space.lg,
    paddingVertical: space.md,
  },
  rowSelf: {
    backgroundColor: colours.ownMuted,
    borderColor: colours.own,
  },
  rankCell: { width: 36, alignItems: 'center' },
  rank: { ...type.numeric, color: colours.textMuted },
  rankSelf: { ...type.numeric, color: colours.accent },
  rowIdentity: { flex: 1, gap: 2 },
  rowName: { ...type.bodyStrong, color: colours.text },
  rowMana: { ...type.caption, color: colours.textFaint },
  rowScore: { alignItems: 'flex-end', gap: 2 },
  rowCount: { ...type.numeric, color: colours.text },
  rowCountUnit: { ...type.caption, color: colours.textFaint },

  centred: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    gap: space.md,
    padding: space.xl,
  },
  subtle: { ...type.body, color: colours.textMuted },
  error: { ...type.body, color: colours.danger, textAlign: 'center' },
  cta: {
    backgroundColor: colours.accent,
    borderRadius: radius.md,
    paddingHorizontal: space.lg,
    paddingVertical: space.md,
  },
  ctaLabel: { ...type.bodyStrong, color: colours.background },
});