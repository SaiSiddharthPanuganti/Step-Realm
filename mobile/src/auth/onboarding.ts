import AsyncStorage from '@react-native-async-storage/async-storage';

/**
 * Remembers whether a player has chosen their display name.
 *
 * Every account starts with a server-derived one (from the email stem), which
 * is fine as a fallback but is not the label a player has consciously picked.
 * The display-name screen is where that choice happens, and this marker is what
 * lets the app know it has already happened for this account.
 *
 * AsyncStorage rather than SecureStore because "did this person finish one
 * screen once" is not a secret, and SecureStore's `WHEN_UNLOCKED` accessibility
 * throws on devices without a passcode. Keyed by user id, the same rule as the
 * step ledger, so two accounts on one phone each keep their own answer.
 */

const KEY_PREFIX = 'steprealm.displayName.v1.';

type AccountId = string | number;

function keyFor(userId: AccountId): string {
  return `${KEY_PREFIX}${userId}`;
}

export async function hasCompletedDisplayName(userId: AccountId): Promise<boolean> {
  const raw = await AsyncStorage.getItem(keyFor(userId));
  return raw === '1';
}

export async function markDisplayNameCompleted(userId: AccountId): Promise<void> {
  await AsyncStorage.setItem(keyFor(userId), '1');
}