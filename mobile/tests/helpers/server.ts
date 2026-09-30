/**
 * Starts a real backend for the contract tests.
 *
 * The alternative -- mocking fetch and asserting on hand-written response
 * objects -- would only prove the client agrees with itself. The types in
 * `src/api/types.ts` are written by hand rather than generated, so what needs
 * proving is that they match what the server actually sends. That is only
 * checkable by talking to the server.
 *
 * Runs against the *test* database rather than the dev one. These tests
 * create real users and claim real tiles, and a dev database that fills up with
 * throwaway accounts is a database nobody trusts. pytest truncates between its
 * own tests, so leftovers here do not leak the other way.
 */

import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';

/** Deliberately not 8000, so these tests never race a running dev server. */
export const CONTRACT_PORT = 8099;
export const CONTRACT_BASE_URL = `http://127.0.0.1:${CONTRACT_PORT}`;

const BACKEND_DIR = path.resolve(__dirname, '..', '..', '..', 'backend');
const PYTHON = path.join(BACKEND_DIR, '.venv', 'Scripts', 'python.exe');

const TEST_DATABASE_URL =
  process.env.TEST_DATABASE_URL ??
  'postgresql+psycopg://postgres:postgres@localhost:5432/steprealm_test';

let server: ChildProcess | null = null;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Poll the liveness endpoint until it answers.
 *
 * Liveness rather than `/health` on purpose: `/health` also checks the database
 * and would keep returning 503 during startup while the schema is being
 * verified, which would look like a hang. All we need here is "the socket is
 * accepting connections"; if the database is genuinely down, the tests will
 * fail with a clear 500 and that is a more honest signal than a timeout.
 */
async function waitForReady(timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let lastError: unknown = null;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${CONTRACT_BASE_URL}/health/live`);
      if (response.ok) {
        return;
      }
    } catch (error) {
      lastError = error;
    }
    await sleep(250);
  }
  throw new Error(
    `Backend did not become ready on ${CONTRACT_BASE_URL} within ${timeoutMs}ms. ` +
      `Last error: ${String(lastError)}. ` +
      `Is PostgreSQL running? Check \`docker compose ps\` in backend/.`,
  );
}

/**
 * Empty the test database.
 *
 * Not housekeeping, a correctness requirement. The world is persistent and
 * shared, so a suite that claims a hardcoded hex passes on a clean database
 * and fails on the second run with "already claimed by another player" -- and
 * only for whoever happens to run it twice. The backend's own pytest suite
 * truncates between tests for the same reason; this makes the contract tests
 * obey the same rule.
 *
 * `alembic_version` is deliberately left alone: truncating it would leave the
 * database looking unmigrated, and the schema is what the next run needs.
 */
function resetDatabase(): void {
  const result = spawnSync(
    'docker',
    [
      'exec', '-i', 'steprealm-postgres',
      'psql', '-U', 'postgres', '-d', 'steprealm_test',
      '-v', 'ON_ERROR_STOP=1',
      '-c',
      'TRUNCATE users, hex_tiles, colleges, rate_limit_counters, walk_progress RESTART IDENTITY CASCADE;',
    ],
    { encoding: 'utf8' },
  );

  if (result.status !== 0) {
    throw new Error(
      'Could not reset the test database. Is Docker running with the ' +
        '`steprealm-postgres` container up? `docker compose ps` in backend/ ' +
        'should list it.\n' +
        `psql said: ${result.stderr || result.stdout}`,
    );
  }
}

export async function setup(): Promise<void> {
  if (!existsSync(PYTHON)) {
    throw new Error(
      `No backend virtualenv at ${PYTHON}. The contract tests need the real ` +
        `server; create it and install requirements before running them.`,
    );
  }

  resetDatabase();

  server = spawn(
    PYTHON,
    ['-m', 'uvicorn', 'app.main:app', '--port', String(CONTRACT_PORT), '--log-level', 'warning'],
    {
      cwd: BACKEND_DIR,
      env: {
        ...process.env,
        DATABASE_URL: TEST_DATABASE_URL,
        // Walkability talks to a third-party public API (Overpass) whose
        // latency and availability are not this suite's business. The client↔
        // server wire contract is what is being pinned down here, and the
        // walkability behaviour itself has its own stubbed tests in the
        // backend's pytest suite -- so a dropped Overpass connection must not
        // be able to fail or slow a wire-format test. The field the feature
        // adds is still asserted by shape.ts, because the schema sends it
        // regardless of whether filtering is enabled.
        WALKABILITY_ENABLED: 'false',
        // Redis is disabled on purpose. The contract server reads dotenv too,
        // so leaving REDIS_URL unset would quietly inherit backend/.env and
        // point it at the *dev* database's cache -- shared state that would
        // make leaderboard assertions depend on what the running dev server
        // has written. An empty value (which the settings layer reads as
        // "not configured") forces the deterministic Postgres fallback; the
        // Redis behaviour itself is covered by pytest against its own test
        // database.
        REDIS_URL: '',
        // The claim-distance gate is a behaviour with its own tests; here it
        // would demand every claim spend a fake walk first. Pinning it to 0
        // keeps the wire-format suite about the wire format, and the field the
        // feature adds is still asserted by shape.ts, because the schema sends
        // it regardless of whether the gate is active.
        MIN_CLAIM_DISTANCE_METERS: '0',
        // Raised to the maximum the backend allows, because a limiter with a
        // low ceiling makes these tests fail for reasons that have nothing to
        // do with the client -- a suite that is rate limited looks exactly like
        // a suite with a broken client. The limiter has its own tests; here it
        // would only get in the way.
        //
        // Signup defaults to 5/minute, which this suite exceeds within seconds.
        AUTH_SIGNUP_RATE_LIMIT_PER_MINUTE: '600',
        AUTH_LOGIN_RATE_LIMIT_PER_MINUTE: '600',
        CLAIM_LOCATION_RATE_LIMIT_PER_MINUTE: '600',
      },
      stdio: 'ignore',
    },
  );

  // Without a listener, a spawn failure (bad interpreter, port taken) becomes
  // an unhandled 'error' event and crashes the worker with a message that
  // names neither the cause nor the port.
  server.on('error', (error) => {
    throw new Error(`Failed to start backend: ${error.message}`);
  });

  await waitForReady(30_000);
}

export async function teardown(): Promise<void> {
  if (server === null) {
    return;
  }
  server.kill();
  // uvicorn shuts down cleanly rather than being killed mid-request, so the
  // database connection is released and Postgres does not log an abort.
  await new Promise<void>((resolve) => {
    if (server === null || server.exitCode !== null) {
      resolve();
      return;
    }
    server.once('exit', () => resolve());
    setTimeout(resolve, 5000);
  });
  server = null;
}
