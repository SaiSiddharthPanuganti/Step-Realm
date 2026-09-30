/** Endpoints for signup, login, reading the current user, and renaming it. */

import type { ApiClient } from './client';
import type { TokenResponse, User } from './types';

export interface SignupInput {
  email: string;
  password: string;
}

/**
 * Create an account and sign in.
 *
 * Marked anonymous so a stale token from a previous session is not attached:
 * the server would otherwise treat this as an authenticated request from
 * whoever was signed in before. A 401 here is the one case where it is not an
 * expired session, it is a wrong password or a taken email.
 */
export function signup(client: ApiClient, input: SignupInput): Promise<TokenResponse> {
  return client.post<TokenResponse>('/auth/signup', input, { anonymous: true });
}

/** Exchange credentials for a token. See `signup` for why this is anonymous. */
export function login(client: ApiClient, input: SignupInput): Promise<TokenResponse> {
  return client.post<TokenResponse>('/auth/login', input, { anonymous: true });
}

/**
 * The signed-in user.
 *
 * Called once at startup to decide whether a stored token is still good. The
 * backend issues no refresh token, so this doubles as the expiry check: a 401
 * here is the signal to clear storage and show the sign-in screen.
 */
export function me(client: ApiClient, signal?: AbortSignal): Promise<User> {
  return client.request<User>('/me', { method: 'GET', signal });
}

/**
 * Choose the display name shown on territory.
 *
 * This is the one write path for the public label. The server enforces the
 * character set and the uniqueness rule; the response is the profile, so the
 * caller can adopt it wholesale rather than reconciling a delta.
 */
export function updateDisplayName(
  client: ApiClient,
  displayName: string,
): Promise<User> {
  return client.patch<User>('/me/display-name', { display_name: displayName });
}
