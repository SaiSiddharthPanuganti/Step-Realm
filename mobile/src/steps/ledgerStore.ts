import AsyncStorage from '@react-native-async-storage/async-storage';

import { EMPTY_LEDGER, type StepLedger } from './checkpoint';

/**
 * Persisting the step ledger across app launches.
 *
 * AsyncStorage rather than SecureStore, on purpose. A step count is not a
 * secret, so the keychain is the wrong tool conceptually, and it is also the
 * wrong tool practically: SecureStore's `WHEN_UNLOCKED` accessibility requires
 * a device passcode and throws without one, which would mean the game silently
 * fails to record walking on a phone with no screen lock set.
 *
 * The ledger is keyed by user id. Two accounts on one phone must not share a
 * step balance: with a single shared key, whichever account happened to sign in
 * last would claim the steps walked while the other was playing.
 */

const KEY_PREFIX = 'steprealm.steps.v1.';

/**
 * The backend's user id is an integer, so this accepts both and normalises. The
 * key is a string either way; doing the conversion here rather than at each call
 * site means a caller cannot accidentally build two different keys for one
 * account and split a step balance in half.
 */
type AccountId = string | number;

function keyFor(userId: AccountId): string {
  return `${KEY_PREFIX}${userId}`;
}

function parse(raw: string | null): StepLedger {
  if (raw === null) {
    return EMPTY_LEDGER;
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== 'object' || parsed === null) {
      return EMPTY_LEDGER;
    }
    const { lastReading, pendingSteps, lastSyncedAt } = parsed as Record<string, unknown>;
    return {
      lastReading: typeof lastReading === 'number' && Number.isFinite(lastReading)
        ? lastReading
        : null,
      pendingSteps: typeof pendingSteps === 'number' && Number.isFinite(pendingSteps) && pendingSteps > 0
        ? pendingSteps
        : 0,
      lastSyncedAt: typeof lastSyncedAt === 'number' ? lastSyncedAt : null,
    };
  } catch {
    // Corrupt or half-written. Starting from empty is the safe direction: it
    // can under-claim, whereas a nonsense baseline would corrupt every delta
    // from here on, and those wrong numbers are what the server sees.
    return EMPTY_LEDGER;
  }
}

export async function loadLedger(userId: AccountId): Promise<StepLedger> {
  return parse(await AsyncStorage.getItem(keyFor(userId)));
}

export async function saveLedger(userId: AccountId, ledger: StepLedger): Promise<void> {
  await AsyncStorage.setItem(keyFor(userId), JSON.stringify(ledger));
}

/**
 * Drop the ledger, on sign-out.
 *
 * Without this the next account to sign in on this device inherits the previous
 * player's unsynced steps and the previous player's pedometer baseline, and
 * would immediately be able to claim a walk it did not take.
 */
export async function clearLedger(userId: AccountId): Promise<void> {
  await AsyncStorage.removeItem(keyFor(userId));
}
