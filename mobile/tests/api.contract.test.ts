/**
 * Contract tests: the client against a real backend.
 *
 * These assert two different things on purpose. That the *shapes* match
 * `src/api/types.ts`, which is what catches hand-written type drift. And that
 * the *error handling* behaves, which is where a mobile client usually goes
 * wrong -- not because the happy path fails, but because it reports a 409 or a
 * 429 to the player as a generic network error.
 */

import { afterAll, describe, expect, it } from 'vitest';

import { ApiClient, ApiError, type TokenStore } from '../src/api/client';
import { login, me, signup } from '../src/api/auth';
import { getWorld } from '../src/api/world';
import { claim, claimByLocation } from '../src/api/territory';
import { syncSteps } from '../src/api/mana';
import { manaWorth } from '../src/steps/checkpoint';
import { CONTRACT_BASE_URL } from './helpers/server';
import {
  assertClaimResponse,
  assertManaSyncResponse,
  assertTokenResponse,
  assertUser,
  assertWorldResponse,
} from './helpers/shape';

/** Token storage held in a Map. Mirrors SecureStore without the native module. */
class MemoryTokenStore implements TokenStore {
  private token: string | null = null;
  async get(): Promise<string | null> {
    return this.token;
  }
  async set(token: string): Promise<void> {
    this.token = token;
  }
  async clear(): Promise<void> {
    this.token = null;
  }
}

let uniqueCounter = 0;

/**
 * A fresh email per test.
 *
 * The users are real rows, so reusing one address would make every later test
 * depend on the earlier ones having run and been cleaned up. Isolation is worth
 * more here than a tidier fixture.
 */
function uniqueEmail(prefix: string): string {
  uniqueCounter += 1;
  return `${prefix}-${Date.now()}-${uniqueCounter}@steprealm.app`;
}

const PASSWORD = 'correct-horse-battery-staple';

function makeClient(): { client: ApiClient; store: MemoryTokenStore; signOuts: () => number } {
  const store = new MemoryTokenStore();
  let signOutCount = 0;
  const client = new ApiClient({
    baseUrl: CONTRACT_BASE_URL,
    tokenStore: store,
    onUnauthorized: () => {
      signOutCount += 1;
    },
  });
  return { client, store, signOuts: () => signOutCount };
}

/** Create an account and return an authenticated client. */
async function signedInClient(prefix: string) {
  const { client, store, signOuts } = makeClient();
  const credentials = { email: uniqueEmail(prefix), password: PASSWORD };
  const token = await signup(client, credentials);
  await store.set(token.access_token);
  return { client, store, signOuts, token, credentials };
}

/**
 * Find an unowned hex near a point.
 *
 * Not an optimisation but a correctness requirement. The world is persistent
 * and shared, so a test that hardcodes `(0, 0)` passes on a fresh database and
 * fails on the second run with "already claimed by another player" -- and worse,
 * only for whoever runs it twice. Asking the server which tiles are actually
 * free is both stable and closer to what the real client has to do.
 */
async function findUnownedTile(
  client: ApiClient,
  near: { lat: number; lon: number },
  rings = 1,
) {
  const world = await getWorld(client, { ...near, rings });
  const free = world.tiles.find((tile) => tile.owner === null);
  if (free === undefined) {
    throw new Error('No unowned tile in viewport; the test world is fully claimed');
  }
  return free;
}

/** Somewhere a brand-new player starts, near the origin but not on it. */
const SPAWN = { lat: 12.9716, lon: 77.5946 };

afterAll(() => {
  // Nothing to tear down: globalSetup stops the server, and these tests create
  // no listeners or timers of their own.
});

describe('auth', () => {
  it('signs up and returns a usable token and user', async () => {
    const { client, store } = makeClient();
    const response = await signup(client, {
      email: uniqueEmail('signup'),
      password: PASSWORD,
    });

    assertTokenResponse(response);
    expect(response.token_type).toBe('bearer');
    expect(response.expires_in_seconds).toBeGreaterThan(0);
    expect(response.user.email).toBe(response.user.email.toLowerCase());

    // The token must work without any further ceremony, or the "included so the
    // client can render a profile immediately" comment is a lie.
    await store.set(response.access_token);
    const current = await me(client);
    assertUser(current);
    expect(current.id).toBe(response.user.id);
  });

  it('rejects a duplicate email with 409 rather than a 500', async () => {
    const credentials = { email: uniqueEmail('dupe'), password: PASSWORD };
    const { client } = makeClient();
    await signup(client, credentials);

    const error = await signup(client, credentials).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(409);
    expect((error as ApiError).isGameRule).toBe(true);
  });

  it('rejects a wrong password with 401 and does not sign the user out', async () => {
    const { credentials } = await signedInClient('wrongpw');
    const { client, signOuts } = makeClient();

    const error = await login(client, { ...credentials, password: 'not-the-password' })
      .catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(401);
    // A mistyped password must not clear the session. Signing the player out
    // here would wipe the form they are in the middle of filling in.
    expect(signOuts()).toBe(0);
  });

  it('signs in again with the right password', async () => {
    const { credentials } = await signedInClient('relogin');
    const { client, store } = makeClient();

    const response = await login(client, credentials);
    assertTokenResponse(response);

    await store.set(response.access_token);
    expect((await me(client)).id).toBe(response.user.id);
  });

  it('signs out when a stored token is rejected', async () => {
    const { client, store, signOuts } = await signedInClient('expired');

    // A syntactically valid but not-issued token, which is what a revoked or
    // server-restarted secret looks like to the client.
    await store.set('not.a.real.token');
    const error = await me(client).catch((e: unknown) => e);

    expect((error as ApiError).status).toBe(401);
    expect((error as ApiError).isAuthFailure).toBe(true);
    expect(signOuts()).toBe(1);
  });
});

describe('world', () => {
  it('reads a viewport anonymously with no viewer', async () => {
    const { client } = makeClient();
    const world = await getWorld(client, { lat: 12.9716, lon: 77.5946, rings: 1 });

    assertWorldResponse(world);
    expect(world.viewer_id).toBeNull();
    expect(world.tiles.length).toBe(world.tile_count);
  });

  it('identifies the viewer when a token is sent', async () => {
    const { client, token } = await signedInClient('viewer');
    const world = await getWorld(client, { lat: 12.9716, lon: 77.5946, rings: 1 });

    assertWorldResponse(world);
    expect(world.viewer_id).toBe(token.user.id);
  });

  it('never sends an email, because the endpoint is public', async () => {
    const { client } = await signedInClient('privacy');
    const target = await findUnownedTile(client, SPAWN);
    await claim(client, { q: target.q, r: target.r });

    const world = await getWorld(client, { lat: target.lat, lon: target.lon, rings: 1 });
    const serialised = JSON.stringify(world);
    // GET /world is anonymous by default, so anything it returns goes to anyone
    // who loads a map. display_name is the only identifying field it may carry.
    expect(serialised).not.toContain('@steprealm.app');
  });
});

describe('territory claims', () => {
  it('spends the starting mana on the first claim', async () => {
    const { client } = await signedInClient('firstclaim');
    const before = await me(client);
    const target = await findUnownedTile(client, SPAWN);

    // A new account holds exactly one claim's worth of mana, and the first
    // claim costs exactly that. Not a coincidence: it is what lets a new player
    // put something of their own on the map immediately, while making that
    // first tile cost something.
    expect(target.owner).toBeNull();
    expect(before.mana).toBe(20);
    expect(before.owned_tile_count).toBe(0);

    const result = await claim(client, { q: target.q, r: target.r });

    assertClaimResponse(result);
    expect(result.tile.is_new).toBe(true);
    expect(result.mana_spent).toBe(result.claim_cost);
    expect(result.mana).toBe(before.mana - result.claim_cost);
    expect(result.owned_tile_count).toBe(1);
  });

  it('treats a repeat claim of an owned tile as a free no-op', async () => {
    const { client } = await signedInClient('retryclaim');
    const target = await findUnownedTile(client, SPAWN);
    const first = await claim(client, { q: target.q, r: target.r });

    const again = await claim(client, { q: target.q, r: target.r });

    assertClaimResponse(again);
    // Re-claiming your own tile costs nothing, so a double-tap or a retried
    // request cannot charge the player twice for one tile.
    expect(again.tile.is_new).toBe(false);
    expect(again.mana_spent).toBe(0);
    expect(again.mana).toBe(first.mana);
    expect(again.owned_tile_count).toBe(1);
  });

  it('costs mana to expand into an adjacent tile', async () => {
    const { client } = await signedInClient('expand');
    // Walk first. The first claim spends the entire starting balance, so there
    // is nothing left to expand with until mana has been earned -- which is the
    // whole reason mana has to come from walking.
    //
    // 2500 steps, not more: a first sync is credited against a 600-second grace
    // period at 5 steps per second, so the server accepts at most 3000 steps
    // however little time has actually passed.
    await syncSteps(client, { steps_delta: 2500 });
    const first = await findUnownedTile(client, SPAWN);
    await claim(client, { q: first.q, r: first.r });

    // Rings 1 around the tile just taken contains its six neighbours, one of
    // which is the tile itself -- so pick a free one and the server will accept
    // it on adjacency. Let the server be the judge: the client never decides
    // which hexes are next to which.
    const neighbour = await findUnownedTile(
      client,
      { lat: first.lat, lon: first.lon },
      1,
    );
    const second = await claim(client, { q: neighbour.q, r: neighbour.r });

    assertClaimResponse(second);
    expect(second.tile.is_new).toBe(true);
    expect(second.mana_spent).toBe(second.claim_cost);
    expect(second.mana).toBeLessThan(second.mana_max);
    expect(second.owned_tile_count).toBe(2);
  });

  it('refuses a non-adjacent tile with 422', async () => {
    const { client } = await signedInClient('nonadjacent');
    // Walked first, so the request fails about adjacency rather than about
    // money. Without mana it would fail with 402 and this test would pass for
    // entirely the wrong reason.
    await syncSteps(client, { steps_delta: 2500 });
    const first = await findUnownedTile(client, SPAWN);
    await claim(client, { q: first.q, r: first.r });

    const far = await findUnownedTile(client, SPAWN, 3);
    const error = await claim(client, { q: far.q + 20, r: far.r + 20 }).catch(
      (e: unknown) => e,
    );

    expect((error as ApiError).status).toBe(422);
    expect((error as ApiError).isGameRule).toBe(true);
    // The message is the explanation the player needs, not a stack trace.
    expect((error as ApiError).detail.length).toBeGreaterThan(0);
  });

  it('claims the hex containing a GPS point', async () => {
    const { client } = await signedInClient('gps');
    // Read a real centre from the world rather than hardcoding a coordinate:
    // the point of the endpoint is that the server decides which hex a
    // coordinate falls in, and a made-up number proves nothing.
    const world = await getWorld(client, { lat: 19.076, lon: 72.8777, rings: 0 });
    const centre = world.centre;

    const result = await claimByLocation(client, { lat: centre.lat, lon: centre.lon });

    assertClaimResponse(result);
    expect(result.tile.is_new).toBe(true);
    expect(result.tile.q).toBe(centre.q);
    expect(result.tile.r).toBe(centre.r);
  });
});

describe('mana', () => {
  it('reports complete state on a sync', async () => {
    const { client } = await signedInClient('mana');
    const result = await syncSteps(client, { steps_delta: 0 });

    assertManaSyncResponse(result);
    expect(result.steps_credited).toBe(0);
    expect(result.pending_step_remainder).toBe(0);
    expect(result.total_mana_gained).toBe(
      result.passive_mana_gained + result.walking_mana_gained,
    );
  });

  it('credits a walking block on a first sync', async () => {
    const { client } = await signedInClient('walk');
    // A full block is 1000 steps for 20 mana. The server skips the speed check
    // for the first 10 minutes after signup, so a brand-new account can sync a
    // block immediately -- and the client should not assume otherwise, or it
    // will refuse to bank the steps a player took before installing.
    const result = await syncSteps(client, { steps_delta: 1000 });

    assertManaSyncResponse(result);
    expect(result.steps_credited).toBe(1000);
    expect(result.walking_mana_gained).toBeGreaterThan(0);
    expect(result.pending_step_remainder).toBe(0);
  });

  it('banks a partial block toward the next one', async () => {
    const { client } = await signedInClient('partial');
    // Fewer than a full block earns nothing but is not discarded. If the
    // remainder were dropped, a walk that ends mid-block would pay nothing and
    // the player would never find out why.
    const result = await syncSteps(client, { steps_delta: 400 });

    assertManaSyncResponse(result);
    expect(result.steps_credited).toBe(400);
    expect(result.pending_step_remainder).toBe(400);
    expect(result.walking_mana_gained).toBe(0);
  });

  it('rejects a delta larger than a first sync can justify', async () => {
    const { client } = await signedInClient('toomany');
    // 5000 steps at 5 steps per second needs 1000 seconds of walking. A first
    // sync is only credited against a 600-second grace, so this is rejected
    // however fresh the account is. The client has to treat a 422 here as a
    // real answer -- resending the same number would just be rejected again --
    // and must not discard the player's steps on the floor.
    const error = await syncSteps(client, { steps_delta: 5000 }).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(422);
    expect((error as ApiError).detail.toLowerCase()).toMatch(/plausible|walked/);
  });

  it('rejects a negative delta with 422', async () => {
    const { client } = await signedInClient('negative');

    const error = await syncSteps(client, { steps_delta: -5 }).catch((e: unknown) => e);

    expect((error as ApiError).status).toBe(422);
  });

  it('credits exactly what the client preview promises', async () => {
    // The sync button renders "+N mana" computed by `manaWorth`, so if that
    // function ever rounds differently from the server the UI makes a promise
    // the server does not keep. Nothing else would catch it: the sync still
    // succeeds, mana still changes, and the only symptom is a number on a
    // button being wrong.
    //
    // A fresh account per case, deliberately. Only a *first* sync gets the
    // 600-second grace, so a second sync on the same account is judged against
    // elapsed time and would be rejected for reasons that have nothing to do
    // with the arithmetic under test.
    for (const steps of [0, 400, 999, 1_000, 1_500, 2_500]) {
      const { client } = await signedInClient(`preview${steps}`);
      const result = await syncSteps(client, { steps_delta: steps });

      expect(result.walking_mana_gained).toBe(manaWorth(steps));
    }
  });

  it('rejects a second sync however small, because the first set the clock', async () => {
    // A consequence of the plausibility check that the client has to design
    // around: eligibility is measured from the last *credited* sync, so once one
    // has happened a follow-up a second later is capped at about five steps and
    // anything larger is a 422. A client that retried a failed sync in a loop, or
    // synced on a timer, would break its own sync button.
    //
    // Pinning this because it is invisible from the client: nothing in the
    // response says "you are now rate limited by walking speed".
    const { client } = await signedInClient('twice');

    await syncSteps(client, { steps_delta: 1_000 });
    const second = await syncSteps(client, { steps_delta: 800 }).catch((e: unknown) => e);

    expect(second).toBeInstanceOf(ApiError);
    expect((second as ApiError).status).toBe(422);
  });

  it('requires a token', async () => {
    const { client } = makeClient();
    const error = await syncSteps(client, { steps_delta: 0 }).catch((e: unknown) => e);
    expect((error as ApiError).status).toBe(401);
  });
});

describe('unreachable server', () => {
  it('reports an offline error rather than a generic failure', async () => {
    // Port 1 is reserved and never listening, so this fails at connect.
    const client = new ApiClient({ baseUrl: 'http://127.0.0.1:1', timeoutMs: 3000 });

    const error = await me(client).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).isOffline).toBe(true);
    expect((error as ApiError).status).toBeNull();
    expect((error as ApiError).userMessage).toMatch(/connection/i);
  });
});
