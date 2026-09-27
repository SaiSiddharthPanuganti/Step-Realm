/**
 * Turning the phone's pedometer into a step delta the server will accept.
 *
 * The server wants steps walked *since the last successful sync*. That sounds
 * like bookkeeping and is actually the whole anti-cheat surface of the client:
 * a client that sends a running total is credited for the same walk on every
 * sync, and a client that resets its counter loses a walk. Both are silent --
 * the player sees a number either way -- so the arithmetic lives here, in pure
 * functions with tests, rather than inside a hook where it cannot be checked.
 *
 * The model is deliberately *differences*, never an absolute total. The
 * installed `expo-sensors` types are explicit that `getStepCountAsync` is
 * `@platform ios` only and throws `UnavailabilityError` on Android, and that
 * `watchStepCount` stops delivering while the app is backgrounded. That leaves
 * two platforms whose cumulative counter is not obviously the same quantity --
 * one where it plausibly means "since boot", one where it may mean "since the
 * watch began". A design that reads the counter absolutely has to guess which,
 * and guessing wrong is unrecoverable: treating a since-watch counter as
 * since-boot means every app launch looks like a counter reset, so every walk
 * is reported as zero and the player earns nothing, forever, with no error
 * anywhere to notice.
 *
 * Accumulating only the difference between readings we actually observed is
 * correct under either interpretation, and gets three other things for free:
 *
 *  - App killed mid-walk. Android kills backgrounded apps freely, and
 *    `pendingSteps` is persisted, so the walk survives.
 *  - A reboot resetting the counter to zero. A lower reading is recognised as a
 *    reset and contributes nothing, rather than subtracting into a negative
 *    delta the server rejects.
 *  - Background and lock gaps, where no reading is delivered. The next reading
 *    simply includes the missed steps, and they count.
 */

/** The server's per-request ceiling, from backend/app/core/constants.py. */
export const MAX_STEPS_PER_SYNC = 250_000;

export interface StepLedger {
  /**
   * The most recent cumulative pedometer value seen, or null before the first.
   *
   * Null is not zero: zero is a legitimate reading, and conflating the two
   * would mean the first sync after install claimed the player's lifetime step
   * count as a single walk.
   */
  lastReading: number | null;
  /** Steps observed but not yet accepted by the server. */
  pendingSteps: number;
  /** Epoch millis of the last successful sync, for the "walked at" display. */
  lastSyncedAt: number | null;
}

export const EMPTY_LEDGER: StepLedger = {
  lastReading: null,
  pendingSteps: 0,
  lastSyncedAt: null,
};

/**
 * Fold a pedometer reading into the ledger.
 *
 * A reading below the last one means the counter reset, which happens on reboot
 * and can happen if the OS re-initialises the sensor. It is anchored and
 * contributes nothing: the steps from before the reset belong to a previous sync
 * or to an unsynced walk whose size is now unknowable, and reporting them
 * upwards would be inventing mana.
 */
export function observe(ledger: StepLedger, reading: number): StepLedger {
  if (!Number.isFinite(reading) || reading < 0) {
    // A nonsensical reading is dropped rather than stored, since a baseline
    // built on it would corrupt every delta that follows.
    return ledger;
  }
  if (ledger.lastReading === null) {
    return { ...ledger, lastReading: reading };
  }
  if (reading < ledger.lastReading) {
    return { ...ledger, lastReading: reading };
  }
  return {
    ...ledger,
    lastReading: reading,
    pendingSteps: ledger.pendingSteps + (reading - ledger.lastReading),
  };
}

/** Fold a sequence of readings, in order. */
export function observeAll(ledger: StepLedger, readings: number[]): StepLedger {
  return readings.reduce(observe, ledger);
}

/**
 * How many steps to put in the next sync.
 *
 * Capped, because the server rejects anything over `MAX_STEPS_PER_SYNC` and
 * would rather reject than silently truncate. The client caps deliberately
 * instead, and the remainder stays pending for the next sync -- see
 * `commitSync`.
 */
export function batchToSend(ledger: StepLedger, maxPerSync = MAX_STEPS_PER_SYNC): number {
  return Math.max(0, Math.min(ledger.pendingSteps, maxPerSync));
}

/**
 * Record that the server accepted `sentSteps`.
 *
 * Only call this after a 2xx. Calling it on a failure is the one way to lose a
 * player's walk for good: the steps would be marked as banked while the server
 * never heard of them, and nothing would tell either side.
 *
 * Only the amount actually sent is deducted, so a capped walk continues from
 * where it left off instead of vanishing.
 */
export function commitSync(
  ledger: StepLedger,
  sentSteps: number,
  now: number,
): StepLedger {
  const settled = Math.max(0, Math.min(sentSteps, ledger.pendingSteps));
  if (settled === 0) {
    // Nothing was credited, so do not stamp the time either -- the UI would
    // otherwise show a sync that credited no steps.
    return ledger;
  }
  return {
    ...ledger,
    pendingSteps: ledger.pendingSteps - settled,
    lastSyncedAt: now,
  };
}

/** Steps observed that the server has not yet been told about. */
export function stepsAwaitingSync(ledger: StepLedger): number {
  return ledger.pendingSteps;
}

/** Whether there is anything worth spending a request on. */
export function hasStepsToSync(ledger: StepLedger): boolean {
  return ledger.pendingSteps > 0;
}

/**
 * Mana a given step total is worth, mirroring the server's arithmetic.
 *
 * Used only to preview the number on the sync button. The server's
 * `floor(balance) * 20` rule means 1,500 steps is worth nothing yet, and
 * showing "20" next to a button that credits 0 would be a lie told by the UI.
 */
export function manaWorth(steps: number, stepsPerBlock = 1_000, manaPerBlock = 20): number {
  return Math.floor(Math.max(0, steps) / stepsPerBlock) * manaPerBlock;
}
