import { defineConfig } from 'vitest/config';

/**
 * Two projects, because they need different worlds.
 *
 * The unit tests are pure functions -- URL resolution, error parsing -- and
 * requiring a running PostgreSQL to check that `10.0.2.2` is the emulator's
 * idea of the host machine would be absurd. The contract tests need a real
 * server, and are the only ones that do.
 */
export default defineConfig({
  test: {
    projects: [
      {
        extends: true,
        test: {
          name: 'unit',
          environment: 'node',
          include: ['tests/unit/**/*.test.ts'],
        },
      },
      {
        extends: true,
        test: {
          name: 'contract',
          environment: 'node',
          include: ['tests/*.contract.test.ts'],
          globalSetup: ['./tests/helpers/server.ts'],
        },
      },
    ],
  },
});
