/**
 * Whether anyone is signed in.
 *
 * A three-state machine rather than a boolean, because "we do not know yet" and
 * "nobody is signed in" are genuinely different situations and the UI treats
 * them differently. A boolean collapses them, and the collapse shows up as a
 * sign-in screen flashing on every cold start while the stored token is being
 * checked -- the exact moment the player is most likely to tap the wrong thing.
 */

import { create } from 'zustand';

import { api, setUnauthorizedHandler } from '../api/runtime';
import {
  login as loginRequest,
  me as fetchMe,
  signup as signupRequest,
  updateDisplayName as updateDisplayNameRequest,
} from '../api/auth';
import { ApiError } from '../api/errors';
import type { User } from '../api/types';
import { SecureTokenStore } from './tokenStore';
import {
  hasCompletedDisplayName,
  markDisplayNameCompleted,
} from './onboarding';

export type SessionStatus = 'restoring' | 'signed-out' | 'signed-in';

export interface Credentials {
  email: string;
  password: string;
}

interface SessionState {
  status: SessionStatus;
  user: User | null;
  /**
   * The player has not yet chosen their display name, so the app must send
   * them through the naming screen before the map. True for any account whose
   * marker has not been stored -- which is exactly every fresh signup.
   */
  needsDisplayName: boolean;
  /** Why the last attempt failed, for display next to the form. */
  error: string | null;
  busy: boolean;

  restore: () => Promise<void>;
  signUp: (credentials: Credentials) => Promise<boolean>;
  signIn: (credentials: Credentials) => Promise<boolean>;
  signOut: () => Promise<void>;
  /** Adopt a fresher user object, e.g. after a claim changed the mana balance. */
  updateUser: (user: User) => void;
  /** Save the chosen display name and clear the onboarding gate. */
  setDisplayName: (displayName: string) => Promise<void>;
}

const store = new SecureTokenStore();

/**
 * Turn an error into something a form can show.
 *
 * 409 and 422 are kept verbatim because the server writes them for the player:
 * "that email is already registered" is more useful than any generic wording
 * this app could invent.
 */
function messageFor(error: unknown): string {
  if (error instanceof ApiError) {
    return error.userMessage;
  }
  return 'Something went wrong. Please try again.';
}

export const useSession = create<SessionState>((set, get) => ({
  status: 'restoring',
  user: null,
  needsDisplayName: false,
  error: null,
  busy: false,

  /**
   * Decide, on cold start, whether the stored token still works.
   *
   * The token itself is not proof. It is a bearer credential with no expiry of
   * its own, so it can be dead for reasons that have nothing to do with the
   * player: the server secret was rotated, the app was reinstalled onto a
   * device whose keychain entry did not survive, or the token was revoked. The
   * only way to know is to spend a round trip on /me.
   *
   * A network failure is treated as *not* signed out. Being unable to reach the
   * server says nothing about whether the token is valid, and signing the
   * player out because they are in a lift would throw away a working session
   * and their unsynced walk with it. So: only a 401 ends the session.
   */
  async restore() {
    set({ status: 'restoring', error: null });

    let token: string | null;
    try {
      token = await store.get();
    } catch {
      // Keychain unreadable -- no device lock screen, or a restored backup.
      set({ status: 'signed-out', user: null });
      return;
    }

    if (token === null) {
      set({ status: 'signed-out', user: null });
      return;
    }

    try {
      const user = await fetchMe(api);
      const needsDisplayName = !(await hasCompletedDisplayName(user.id));
      set({ status: 'signed-in', user, needsDisplayName });
    } catch (error) {
      if (error instanceof ApiError && error.isAuthFailure) {
        await store.clear().catch(() => undefined);
        set({ status: 'signed-out', user: null });
        return;
      }
      // Offline or a server fault. Keep whatever we have, but do not claim to be
      // signed in either -- the screens behind this state need a real user.
      set({ status: 'signed-out', user: null, error: messageFor(error) });
    }
  },

  async signUp(credentials) {
    return authenticate(signupRequest, credentials, set);
  },

  async signIn(credentials) {
    return authenticate(loginRequest, credentials, set);
  },

  async signOut() {
    await store.clear().catch(() => undefined);
    set({ status: 'signed-out', user: null, needsDisplayName: false, error: null });
  },

  updateUser(user) {
    set({ user });
  },

  async setDisplayName(displayName) {
    const user = await updateDisplayNameRequest(api, displayName);
    await markDisplayNameCompleted(user.id);
    set({ user, needsDisplayName: false });
  },
}));

/**
 * Shared body of sign-in and sign-up, because the two are the same operation
 * from the app's point of view and any difference between them would be a bug
 * waiting to be found.
 */
async function authenticate(
  request: (client: typeof api, credentials: Credentials) => Promise<{
    access_token: string;
    user: User;
  }>,
  credentials: Credentials,
  set: (partial: Partial<SessionState>) => void,
): Promise<boolean> {
  set({ busy: true, error: null });
  try {
    const response = await request(api, credentials);
    // Store the token before announcing the session, so a crash between the
    // two leaves a recoverable state (signed out) rather than a signed-in UI
    // with no token behind it.
    await store.set(response.access_token);
    const needsDisplayName = !(await hasCompletedDisplayName(response.user.id));
    set({ status: 'signed-in', user: response.user, needsDisplayName, busy: false, error: null });
    return true;
  } catch (error) {
    set({ busy: false, error: messageFor(error) });
    return false;
  }
}

// A 401 anywhere means the session is over. Registered once, at module load,
// so no component has to remember to wire it.
setUnauthorizedHandler(() => {
  const { status } = useSession.getState();
  if (status === 'signed-in') {
    void useSession.getState().signOut();
  }
});
