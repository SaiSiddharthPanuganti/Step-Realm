/**
 * The profile screen: the signed-in player's account.
 *
 * Everything here is read-mostly, and every number on the card is refetched
 * when the screen opens rather than trusted from the session -- mana regens
 * passively and claims change the tile count while the player is on the map,
 * so the session's copy is stale by definition. The sign-out action, which
 * used to crowd the map's title bar, lives here: signing out is an account
 * decision, not a navigation one.
 */

import { useEffect } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useRouter } from 'expo-router';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { useSession } from '../auth/session';
import { useProfile } from '../state/world';
import { colours, radius, space, type } from '../ui/theme';

export default function ProfileScreen() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const status = useSession((state) => state.status);
  const sessionUser = useSession((state) => state.user);
  const signOut = useSession((state) => state.signOut);
  const profile = useProfile();

  // The session can end while this screen is mounted (a 401 anywhere signs
  // the player out), and there is no profile to show then.
  useEffect(() => {
    if (status === 'signed-out') {
      router.replace('/sign-in');
      return;
    }
    // Settle the balance the way GET /me settles it, so the mana shown is the
    // number the walk-ledger would quote -- not the value captured at login.
    profile.refetch().catch(() => undefined);
  }, [status, router, profile]);

  const user = profile.data ?? sessionUser;

  if (user === null || status === 'signed-out') {
    return <View style={[styles.root, { paddingTop: insets.top }]} />;
  }

  const manaFraction = Math.max(0, Math.min(1, user.mana / user.mana_max));

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
        <Text style={styles.headerTitle}>Profile</Text>
      </View>

      <ScrollView contentContainerStyle={styles.content}>
        <View style={styles.hero}>
          <View style={styles.avatar}>
            <Text style={styles.avatarGlyph}>{user.display_name.slice(0, 1).toUpperCase()}</Text>
          </View>
          <Text style={styles.name}>{user.display_name}</Text>
          <Text style={styles.email}>{user.email}</Text>
        </View>

        <View style={styles.card}>
          <View style={styles.cardRow}>
            <Text style={styles.cardLabel}>Mana</Text>
            <Text style={styles.manaValue}>
              {user.mana}
              <Text style={styles.manaValueMuted}> / {user.mana_max}</Text>
            </Text>
          </View>
          <View style={styles.track} accessibilityRole="progressbar">
            <View style={[styles.manaFill, { width: `${manaFraction * 100}%` }]} />
          </View>
          <Text style={styles.cardFine}>
            Walk a metre to earn a mana; claims cost 20.
          </Text>
        </View>

        <View style={styles.card}>
          <View style={styles.cardRow}>
            <Text style={styles.cardLabel}>Hexes claimed</Text>
            <Text style={styles.cardValue}>
              {user.owned_tile_count}
              <Text style={styles.cardValueMuted}>
                {user.owned_tile_count === 1 ? ' hex' : ' hexes'}
              </Text>
            </Text>
          </View>
          {user.college !== null ? (
            <View style={styles.cardRow}>
              <Text style={styles.cardLabel}>College</Text>
              <Text style={styles.cardValue}>{user.college.name}</Text>
            </View>
          ) : null}
        </View>

        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Sign out"
          style={({ pressed }) => [styles.signOut, pressed && styles.pressed]}
          onPress={() => {
            void signOut();
            router.replace('/sign-in');
          }}
        >
          <Text style={styles.signOutLabel}>Sign out</Text>
        </Pressable>
      </ScrollView>
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
  headerTitle: { ...type.heading, color: colours.text },
  pressed: { opacity: 0.8 },

  content: {
    paddingHorizontal: space.lg,
    paddingBottom: space.xxl,
    gap: space.lg,
  },

  hero: {
    alignItems: 'center',
    gap: space.xs,
    paddingVertical: space.lg,
  },
  avatar: {
    width: 72,
    height: 72,
    borderRadius: 36,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: colours.ownMuted,
    borderWidth: 1,
    borderColor: colours.border,
    marginBottom: space.sm,
  },
  avatarGlyph: { ...type.display, color: colours.accent },
  name: { ...type.title, color: colours.text },
  email: { ...type.caption, color: colours.textMuted },

  card: {
    backgroundColor: colours.surfaceRaised,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colours.border,
    padding: space.lg,
    gap: space.md,
  },
  cardRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: space.md,
  },
  cardLabel: { ...type.label, color: colours.textMuted },
  cardValue: { ...type.numeric, color: colours.text },
  cardValueMuted: { ...type.numeric, color: colours.textFaint, fontSize: 14 },
  manaValue: { ...type.numeric, color: colours.mana },
  manaValueMuted: { ...type.numeric, color: colours.textFaint, fontSize: 14 },
  track: {
    height: 8,
    borderRadius: radius.pill,
    backgroundColor: colours.surface,
    overflow: 'hidden',
  },
  manaFill: { height: '100%', borderRadius: radius.pill, backgroundColor: colours.mana },
  cardFine: { ...type.caption, color: colours.textFaint },

  signOut: {
    borderWidth: 1,
    borderColor: colours.danger,
    borderRadius: radius.md,
    paddingVertical: space.md,
    alignItems: 'center',
    marginTop: space.sm,
  },
  signOutLabel: { ...type.bodyStrong, color: colours.danger },
});