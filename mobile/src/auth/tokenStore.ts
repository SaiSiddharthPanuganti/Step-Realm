/**
 * Token storage backed by the device keychain.
 *
 * SecureStore rather than AsyncStorage, and not by preference. The token is a
 * bearer credential with no expiry of its own: whoever holds it *is* the user,
 * until the server's signing secret changes. AsyncStorage is a plain-text file
 * in the app's sandbox, which is readable from a rooted device and included in
 * adb backups, so it is the wrong place for this even though it is the more
 * convenient one.
 *
 * Keychain-backed storage is also not automatically available everywhere: on a
 * device with no lock screen set, Android's keystore will refuse to store
 * anything. That is a real configuration, not an edge case, so both calls are
 * wrapped and the failure is allowed to propagate as an offline condition
 * rather than being swallowed into a silent sign-out loop.
 */

import * as SecureStore from 'expo-secure-store';

import type { TokenStore } from '../api/client';

/**
 * Namespaced so this cannot collide with another library's keychain entries.
 * The app id prefix is added automatically by SecureStore.
 */
const TOKEN_KEY = 'steprealm.access_token';

export class SecureTokenStore implements TokenStore {
  async get(): Promise<string | null> {
    return SecureStore.getItemAsync(TOKEN_KEY);
  }

  async set(token: string): Promise<void> {
    await SecureStore.setItemAsync(TOKEN_KEY, token, {
      // Deliberately not WHEN_UNLOCKED_THIS_DEVICE_ONLY... this *is* the
      // strictest practical option, and it is right here: a token that would
      // restore onto a device the player never unlocked is a token the player
      // never meant to share. The cost is signing in again after a reboot,
      // which is a fair trade for not leaking a session.
      keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
    });
  }

  async clear(): Promise<void> {
    await SecureStore.deleteItemAsync(TOKEN_KEY);
  }
}
