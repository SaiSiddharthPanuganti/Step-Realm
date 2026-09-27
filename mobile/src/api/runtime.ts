/**
 * Wires the real platform into the platform-free API layer.
 *
 * Everything under `src/api/` takes its base URL, token store and callbacks as
 * arguments so it can be tested in Node. This is the one file that knows about
 * Expo modules, which is what keeps that testability from being a fiction.
 */

import Constants from 'expo-constants';
import { Platform } from 'react-native';

import { ApiClient, type TokenStore } from './client';
import { resolveBaseUrl } from './config';
import { SecureTokenStore } from '../auth/tokenStore';

/**
 * The dev server address, which is the host the phone already reached to
 * download the bundle. Null on web.
 */
function devServerHost(): string | null {
  const hostUri = Constants.expoConfig?.hostUri;
  return typeof hostUri === 'string' ? hostUri : null;
}

export const API_BASE_URL = resolveBaseUrl({
  envUrl: process.env.EXPO_PUBLIC_API_URL ?? null,
  platform: Platform.OS === 'ios' || Platform.OS === 'android' ? Platform.OS : 'web',
  hostUri: devServerHost(),
});

/** Called when the server rejects a stored token. Wired to the session store. */
let onUnauthorized: () => void = () => {};

/**
 * Register the sign-out callback.
 *
 * A setter rather than a constructor argument because the API client is a
 * module-level singleton created before React mounts, and the session store
 * that owns the sign-out logic is created later. Registering here keeps the
 * dependency pointing one way: the API layer asks to be told, it does not reach
 * into application state.
 */
export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler;
}

export function createApiClient(tokenStore: TokenStore = new SecureTokenStore()): ApiClient {
  return new ApiClient({
    baseUrl: API_BASE_URL,
    tokenStore,
    onUnauthorized: () => onUnauthorized(),
  });
}

/** The client the app uses. Tests build their own with a stub store. */
export const api = createApiClient();
