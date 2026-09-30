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
import { login, me, signup, updateDisplayName } from '../src/api/auth';
import { getWorld } from '../src/api/world';
import { getLeaderboard } from '../src/api/leaderboard';
import { claim, claimByLocation, walkReport } from '../src/api/territory';
import { CONTRACT_BASE_URL } from './helpers/server';
import {
  assertClaimResponse,
  assertLeaderboardResponse,
  assertTokenResponse,
  assertUser,
  assertWalkReportResponse,
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

/**
 * A city far from `SPAWN`, used by tests that claim several hexes.
 *
 * The contract world is shared and persistent for one suite run: the claim
 * tests cluster around `SPAWN` and leave nothing unowned in its first ring
 * by the end. Tests that need headroom claim in a city nobody else touches,
 * so the number of free tiles each one can use is not bounded by how many
 * earlier tests ran.
 */
const BOARD_CITY = { lat: 35.6762, lon: 139.6503 };

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

  it('reports the claim-distance gate and per-viewer walk totals', async () => {
    // The contract server pins MIN_CLAIM_DISTANCE_METERS to 0, so the gate's
    // field must exist and say 0 -- and no viewer should leak another's walk
    // metres, least of all anonymously.
    const { client } = makeClient();
    const world = await getWorld(client, { lat: 12.9716, lon: 77.5946, rings: 1 });

    assertWorldResponse(world);
    expect(world.claim_required_meters).toBe(0);
    for (const tile of world.tiles) {
      expect(tile.walk_meters).toBe(0);
    }
  });

  it('never sends an email, because the endpoint is public', async () => {
    const { client } = await signedInClient('privacy');
    const target = await findUnownedTile(client, SPAWN);
    await walkReport(client, { lat: target.lat, lon: target.lon, meters: 20 });
    await claim(client, { q: target.q, r: target.r });

    const world = await getWorld(client, { lat: target.lat, lon: target.lon, rings: 1 });
    const serialised = JSON.stringify(world);
    // GET /world is anonymous by default, so anything it returns goes to anyone
    // who loads a map. display_name is the only identifying field it may carry.
    expect(serialised).not.toContain('@steprealm.app');
  });
});

describe('walk progress', () => {
  it('banks metres into the hex the server projects the point into', async () => {
    const { client } = await signedInClient('walkbank');
    // Read a real centre rather than hardcoding a coordinate: the point of the
    // endpoint is that the server decides which hex a point falls in.
    const world = await getWorld(client, { lat: 19.076, lon: 72.8777, rings: 0 });
    const centre = world.centre;

    const result = await walkReport(client, {
      lat: centre.lat,
      lon: centre.lon,
      meters: 5,
    });

    assertWalkReportResponse(result);
    expect(result.q).toBe(centre.q);
    expect(result.r).toBe(centre.r);
    expect(result.total).toBe(5);
    expect(result.required).toBe(0);
    expect(result.remaining).toBe(0);
    // One metre is one mana, and a fresh account in its first report (still
    // inside the signup grace) is entirely walking credit.
    expect(result.walking_gained).toBe(5);
    expect(result.mana).toBe(5);
  });

  it('accumulates across reports in the same hex', async () => {
    const { client } = await signedInClient('walktotal');
    const world = await getWorld(client, { lat: 19.076, lon: 72.8777, rings: 0 });
    const centre = world.centre;

    await walkReport(client, { lat: centre.lat, lon: centre.lon, meters: 4 });
    // The speed gate gives even a back-to-back request a minimum one-second
    // window, so a report of 6 m only fits once the wall clock has actually
    // moved forward that far. Give the second report real elapsed time instead
    // of fighting the gate.
    await new Promise((resolve) => setTimeout(resolve, 1700));
    const second = await walkReport(client, { lat: centre.lat, lon: centre.lon, meters: 6 });

    assertWalkReportResponse(second);
    expect(second.total).toBe(10);
  });

  it('rejects a walking speed no human could maintain', async () => {
    const { client } = await signedInClient('walkspeed');
    const world = await getWorld(client, { lat: 19.076, lon: 72.8777, rings: 0 });
    const centre = world.centre;

    await walkReport(client, { lat: centre.lat, lon: centre.lon, meters: 5 });
    // An immediate 80 m claims a sprint across a campus in the milliseconds the
    // two requests were apart. The client has to treat a 422 here as a real
    // answer -- resending the same metres would just be rejected again -- and
    // must not discard the walk on the floor.
    const error = await walkReport(client, { lat: centre.lat, lon: centre.lon, meters: 80 }).catch(
      (e: unknown) => e,
    );

    expect((error as ApiError).status).toBe(422);
    expect((error as ApiError).isGameRule).toBe(true);
    expect((error as ApiError).detail.toLowerCase()).toMatch(/plausible/);
  });

  it('rejects a zero delta', async () => {
    const { client } = await signedInClient('walkzero');
    const error = await walkReport(client, { lat: 0, lon: 0, meters: 0 }).catch(
      (e: unknown) => e,
    );
    expect((error as ApiError).status).toBe(422);
  });

  it('requires a token', async () => {
    const { client } = makeClient();
    const error = await walkReport(client, { lat: 0, lon: 0, meters: 5 }).catch(
      (e: unknown) => e,
    );
    expect((error as ApiError).status).toBe(401);
  });
});

describe('territory claims', () => {
  it('starts with no mana and funds the first claim by walking', async () => {
    const { client } = await signedInClient('firstclaim');
    const target = await findUnownedTile(client, SPAWN);
    const before = await me(client);

    expect(target.owner).toBeNull();
    expect(before.mana).toBe(0);
    expect(before.owned_tile_count).toBe(0);

    // Mana has exactly one source -- walking -- so a claim attempt before any
    // walking is refused for lack of funds rather than silently approved.
    const empty = await claim(client, { q: target.q, r: target.r }).catch((e: unknown) => e);
    expect((empty as ApiError).status).toBe(402);

    // Walk a claim's worth (1 m = 1 mana) and the same claim now succeeds.
    await walkReport(client, { lat: target.lat, lon: target.lon, meters: 20 });
    const result = await claim(client, { q: target.q, r: target.r });

    assertClaimResponse(result);
    expect(result.tile.is_new).toBe(true);
    expect(result.mana_spent).toBe(result.claim_cost);
    expect(result.mana).toBe(0);
    expect(result.owned_tile_count).toBe(1);
  });

  it('treats a repeat claim of an owned tile as a free no-op', async () => {
    const { client } = await signedInClient('retryclaim');
    const target = await findUnownedTile(client, SPAWN);
    await walkReport(client, { lat: target.lat, lon: target.lon, meters: 20 });
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
    // Walk first. Two claims to make and every claim costs one tile's worth of
    // walking, so two claims' worth of metres funds both, in one report.
    const first = await findUnownedTile(client, SPAWN);
    const around = await getWorld(client, { lat: first.lat, lon: first.lon, rings: 1 });
    const neighbour = around.tiles.find(
      (tile) => tile.owner === null && !(tile.q === first.q && tile.r === first.r),
    );
    if (neighbour === undefined) {
      throw new Error('No unowned neighbour near the target tile');
    }
    await walkReport(client, { lat: first.lat, lon: first.lon, meters: 40 });
    const firstClaim = await claim(client, { q: first.q, r: first.r });
    const second = await claim(client, { q: neighbour.q, r: neighbour.r });

    assertClaimResponse(second);
    expect(second.tile.is_new).toBe(true);
    expect(second.mana_spent).toBe(second.claim_cost);
    expect(firstClaim.mana - second.claim_cost).toBe(second.mana);
    expect(second.owned_tile_count).toBe(2);
  });

  it('refuses a non-adjacent tile with 422', async () => {
    const { client } = await signedInClient('nonadjacent');
    // Walked first, so the request fails about adjacency rather than about
    // money. Without mana it would fail with 402 and this test would pass for
    // entirely the wrong reason.
    const first = await findUnownedTile(client, SPAWN);
    await walkReport(client, { lat: first.lat, lon: first.lon, meters: 40 });
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

    // A centre tile starts unowned and there is no generic gate on the
    // contract server, so bank a claim's worth at the point before spending it.
    await walkReport(client, { lat: centre.lat, lon: centre.lon, meters: 20 });
    const walked = await me(client);
    expect(walked.mana).toBeGreaterThanOrEqual(20);

    const result = await claimByLocation(client, { lat: centre.lat, lon: centre.lon });

    assertClaimResponse(result);
    expect(result.tile.is_new).toBe(true);
    expect(result.tile.q).toBe(centre.q);
    expect(result.tile.r).toBe(centre.r);
  });
});

describe('mana from walking', () => {
  it('reports complete mana state on a walk report', async () => {
    const { client } = await signedInClient('walkmana');
    const world = await getWorld(client, { lat: 19.076, lon: 72.8777, rings: 0 });
    const centre = world.centre;

    const result = await walkReport(client, { lat: centre.lat, lon: centre.lon, meters: 3 });

    assertWalkReportResponse(result);
    expect(result.walking_gained).toBe(3);
    expect(result.passive_gained).toBe(0);
    expect(result.total_mana_gained).toBe(
      result.walking_gained + result.passive_gained,
    );
    expect(result.mana).toBe(result.total_mana_gained);
  });

  it('credits one mana per metre walked', async () => {
    const { client } = await signedInClient('permetre');
    const world = await getWorld(client, { lat: 19.076, lon: 72.8777, rings: 0 });
    const centre = world.centre;

    const result = await walkReport(client, { lat: centre.lat, lon: centre.lon, meters: 25 });

    assertWalkReportResponse(result);
    expect(result.walking_gained).toBe(25);
    expect(result.daily_mana_earned).toBe(25);
  });

  it('counts walking against the daily cap', async () => {
    const { client } = await signedInClient('daily');
    const world = await getWorld(client, { lat: 19.076, lon: 72.8777, rings: 0 });
    const centre = world.centre;

    const first = await walkReport(client, { lat: centre.lat, lon: centre.lon, meters: 25 });
    // Give the plausibility clock a real second before the next report so the
    // request is judged on elapsed time, not on the millisecond it took to run
    // -- and walk slowly enough that the speed gate accepts it.
    await new Promise((resolve) => setTimeout(resolve, 1700));
    const second = await walkReport(client, { lat: centre.lat, lon: centre.lon, meters: 4 });

    assertWalkReportResponse(second);
    expect(first.daily_mana_earned).toBe(25);
    expect(second.daily_mana_earned).toBe(29);
    // What is left of the cap budget goes down by exactly what was earned.
    expect(second.daily_mana_remaining).toBe(first.daily_mana_remaining - 4);
    expect(second.total_mana_gained).toBe(4);
  });

  it('rejects a zero delta', async () => {
    const { client } = await signedInClient('walkzero');
    const error = await walkReport(client, { lat: 0, lon: 0, meters: 0 }).catch(
      (e: unknown) => e,
    );
    expect((error as ApiError).status).toBe(422);
  });

  it('requires a token', async () => {
    const { client } = makeClient();
    const error = await walkReport(client, { lat: 0, lon: 0, meters: 5 }).catch(
      (e: unknown) => e,
    );
    expect((error as ApiError).status).toBe(401);
  });
});

describe('display name', () => {
  it('renames the account and the profile reflects it', async () => {
    const { client } = await signedInClient('rename');
    const renamed = await updateDisplayName(client, 'TheFirstWalker');

    assertUser(renamed);
    expect(renamed.display_name).toBe('TheFirstWalker');
    expect((await me(client)).display_name).toBe('TheFirstWalker');
  });

  it('quotes back the derived name until a name is chosen', async () => {
    const { client } = await signedInClient('derived');
    // The server stamps a name from the email stem at signup, so `me` never
    // comes back empty -- the map has a label to draw either way.
    const current = await me(client);
    expect(current.display_name.length).toBeGreaterThan(0);
    expect(current.display_name.length).toBeLessThanOrEqual(24);
  });

  it('rejects a name another player holds with 409', async () => {
    const a = await signedInClient('holder');
    const b = await signedInClient('taker');
    await updateDisplayName(a.client, 'ReservedOne');

    const conflict = await updateDisplayName(b.client, 'ReservedOne').catch(
      (e: unknown) => e,
    );
    expect((conflict as ApiError).status).toBe(409);
    expect((conflict as ApiError).isGameRule).toBe(true);
  });

  it('rejects characters and lengths the server will not store', async () => {
    const { client } = await signedInClient('invalidname');

    const spaced = await updateDisplayName(client, 'bad name!').catch((e: unknown) => e);
    expect((spaced as ApiError).status).toBe(422);

    const tooShort = await updateDisplayName(client, 'ab').catch((e: unknown) => e);
    expect((tooShort as ApiError).status).toBe(422);
  });

  it('requires a token', async () => {
    const { client } = makeClient();
    const error = await updateDisplayName(client, 'Anonymous').catch((e: unknown) => e);
    expect((error as ApiError).status).toBe(401);
  });
});

describe('leaderboard', () => {
  it('posts a claimer onto the board and pins the viewer unranked', async () => {
    const claimer = await signedInClient('rankclaim');
    const target = await findUnownedTile(claimer.client, BOARD_CITY, 2);
    await walkReport(claimer.client, { lat: target.lat, lon: target.lon, meters: 20 });
    await claim(claimer.client, { q: target.q, r: target.r });

    const onlooker = await signedInClient('rankview');
    const board = await getLeaderboard(onlooker.client, 100);

    assertLeaderboardResponse(board);
    // The board is shared and keeps every player who claimed before page
    // load, so it asserts *positioning*, not emptiness: the fresh claimer is
    // on the board owning exactly one hex, and the list is ordered by
    // territory descending.
    expect(board.entries.length).toBeGreaterThan(0);
    const mine = board.entries.find((entry) => entry.user_id === claimer.token.user.id);
    expect(mine?.owned_tile_count).toBe(1);
    expect(mine?.rank).toBeGreaterThanOrEqual(1);
    const counts = board.entries.map((entry) => entry.owned_tile_count);
    expect([...counts].sort((a, b) => b - a)).toEqual(counts);
    // The caller's own standing travels with the response even though they
    // own nothing: unranked, not "last".
    expect(board.viewer?.user_id).toBe(onlooker.token.user.id);
    expect(board.viewer?.owned_tile_count).toBe(0);
    expect(board.viewer?.rank).toBeNull();
    expect(board.degraded).toBe(true);
  });

  it('orders claimers by territory descending with the stable tie-break', async () => {
    const one = await signedInClient('boar');
    const two = await signedInClient('border');
    const first = await findUnownedTile(one.client, BOARD_CITY, 2);
    await walkReport(one.client, { lat: first.lat, lon: first.lon, meters: 40 });
    await claim(one.client, { q: first.q, r: first.r });
    const around = await getWorld(one.client, { lat: first.lat, lon: first.lon, rings: 1 });
    const neighbour = around.tiles.find(
      (tile) => tile.owner === null && !(tile.q === first.q && tile.r === first.r),
    );
    if (neighbour === undefined) {
      throw new Error('No unowned neighbour near the target tile');
    }
    await claim(one.client, { q: neighbour.q, r: neighbour.r });

    const second = await findUnownedTile(two.client, BOARD_CITY, 2);
    await walkReport(two.client, { lat: second.lat, lon: second.lon, meters: 40 });
    await claim(two.client, { q: second.q, r: second.r });
    const aroundTwo = await getWorld(two.client, { lat: second.lat, lon: second.lon, rings: 1 });
    const neighbourTwo = aroundTwo.tiles.find(
      (tile) => tile.owner === null && !(tile.q === second.q && tile.r === second.r),
    );
    if (neighbourTwo === undefined) {
      throw new Error('No unowned neighbour near the second target tile');
    }
    await claim(two.client, { q: neighbourTwo.q, r: neighbourTwo.r });

    const board = await getLeaderboard(one.client, 100);

    assertLeaderboardResponse(board);
    // Territory descending, no ties out of order anywhere in the list.
    const counts = board.entries.map((entry) => entry.owned_tile_count);
    expect([...counts].sort((a, b) => b - a)).toEqual(counts);
    // Both leaders hold two hexes; the shared board may hold other two-hex
    // players too, but between a tied pair the stable user-id order decides,
    // and neither leader can rank below a one-hex player.
    const oneIndex = board.entries.findIndex((entry) => entry.user_id === one.token.user.id);
    const twoIndex = board.entries.findIndex((entry) => entry.user_id === two.token.user.id);
    expect(oneIndex).toBeGreaterThan(-1);
    expect(twoIndex).toBeGreaterThan(-1);
    expect(oneIndex).toBeLessThan(twoIndex);
    expect(board.entries[oneIndex].owned_tile_count).toBe(2);
    expect(board.entries[twoIndex].owned_tile_count).toBe(2);
    expect(board.viewer?.user_id).toBe(one.token.user.id);
    expect(board.viewer?.owned_tile_count).toBe(2);
    expect(board.viewer?.rank).toBeGreaterThanOrEqual(1);
  });

  it('requires a token', async () => {
    const { client } = makeClient();
    const error = await getLeaderboard(client).catch((e: unknown) => e);
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
