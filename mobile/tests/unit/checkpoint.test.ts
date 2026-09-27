import { describe, expect, it } from 'vitest';

import {
  EMPTY_LEDGER,
  MAX_STEPS_PER_SYNC,
  batchToSend,
  commitSync,
  hasStepsToSync,
  manaWorth,
  observe,
  observeAll,
  stepsAwaitingSync,
  type StepLedger,
} from '../../src/steps/checkpoint';

/** A ledger that has been anchored on a reading but synced nothing. */
const anchoredAt = (reading: number, pendingSteps = 0): StepLedger => ({
  lastReading: reading,
  pendingSteps,
  lastSyncedAt: null,
});

describe('observe', () => {
  it('claims nothing on the first reading, rather than the lifetime step count', () => {
    // The counter plausibly includes every step since the phone booted. A
    // player who has walked 40,000 steps today and is installing the game has
    // not walked 40,000 steps *for this game*, and the first sync must not say
    // they have.
    const ledger = observe(EMPTY_LEDGER, 40_000);
    expect(stepsAwaitingSync(ledger)).toBe(0);
  });

  it('anchors the first reading so later deltas have something to compare to', () => {
    expect(observe(EMPTY_LEDGER, 40_000).lastReading).toBe(40_000);
  });

  it('accumulates the difference between readings', () => {
    const ledger = observeAll(EMPTY_LEDGER, [1_000, 1_200, 1_500]);
    expect(stepsAwaitingSync(ledger)).toBe(500);
  });

  it('counts a walk reported in a single large jump', () => {
    // The pedometer batches, and after a lock screen it may deliver one reading
    // covering the whole gap. The whole difference is still real walking.
    const ledger = observe(anchoredAt(1_000), 3_400);
    expect(stepsAwaitingSync(ledger)).toBe(2_400);
  });

  it('contributes nothing when the reading has not moved', () => {
    expect(stepsAwaitingSync(observe(anchoredAt(1_000), 1_000))).toBe(0);
  });
});

describe('a counter reset', () => {
  it('claims nothing rather than reporting a negative walk', () => {
    // Both platforms reset the counter on reboot. Subtracting here would give
    // -4000, which the server rejects as invalid -- so the player gets an error
    // instead of a sync and the app looks broken.
    expect(stepsAwaitingSync(observe(anchoredAt(5_000), 1_000))).toBe(0);
  });

  it('re-anchors onto the new value', () => {
    // Without this, every later sync would report zero forever: the stored
    // baseline is a value the device no longer has.
    expect(observe(anchoredAt(5_000), 1_000).lastReading).toBe(1_000);
  });

  it('lets the player earn again after the reboot', () => {
    const afterReboot = observe(anchoredAt(5_000), 1_000);
    expect(stepsAwaitingSync(observe(afterReboot, 1_400))).toBe(400);
  });

  it('does not invent steps for the walk that was lost with the counter', () => {
    expect(stepsAwaitingSync(observe(anchoredAt(5_000), 1_000))).not.toBe(4_000);
  });
});

describe('observe rejects nonsense', () => {
  it('ignores a non-finite reading', () => {
    const before = anchoredAt(1_000);
    expect(observe(before, Number.NaN)).toBe(before);
  });

  it('ignores a negative reading', () => {
    const before = anchoredAt(1_000);
    expect(observe(before, -1)).toBe(before);
  });

  it('ignores Infinity', () => {
    const before = anchoredAt(1_000);
    expect(observe(before, Number.POSITIVE_INFINITY)).toBe(before);
  });
});

describe('batchToSend', () => {
  it('sends an ordinary walk in full', () => {
    expect(batchToSend(anchoredAt(0, 1_200))).toBe(1_200);
  });

  it('truncates a walk larger than one sync can carry', () => {
    expect(batchToSend(anchoredAt(0, 400_000))).toBe(MAX_STEPS_PER_SYNC);
  });

  it('sends nothing when there is nothing', () => {
    expect(batchToSend(anchoredAt(1_000))).toBe(0);
  });
});

describe('commitSync', () => {
  const NOW = 2_000;

  it('deducts exactly what was sent', () => {
    const after = commitSync(anchoredAt(0, 1_500), 1_500, NOW);
    expect(stepsAwaitingSync(after)).toBe(0);
  });

  it('leaves the remainder of a capped walk claimable', () => {
    // Re-anchoring on the reading would bank the remaining 150,000 as though
    // the server had heard of them, and the player would silently lose 3000
    // mana with nothing on screen to explain it.
    const after = commitSync(anchoredAt(0, 400_000), MAX_STEPS_PER_SYNC, NOW);
    expect(stepsAwaitingSync(after)).toBe(150_000);
  });

  it('stamps the sync time on a successful sync', () => {
    expect(commitSync(anchoredAt(0, 500), 500, NOW).lastSyncedAt).toBe(NOW);
  });

  it('changes nothing when nothing was sent', () => {
    // A sync that credited no steps must not look like a successful sync in
    // the UI, so the timestamp is left alone too.
    const before = anchoredAt(1_000);
    expect(commitSync(before, 0, NOW)).toBe(before);
  });

  it('never deducts more than is pending', () => {
    // Guards the "sent" value being wrong, e.g. a retried request whose
    // response arrived twice. A negative pending count would then read as a
    // giant credit on the next sync.
    expect(stepsAwaitingSync(commitSync(anchoredAt(0, 100), 5_000, NOW))).toBe(0);
  });
});

describe('a walk split across two capped syncs', () => {
  it('reports every step exactly once', () => {
    // The property that matters: capping and committing must partition the walk.
    // If the remainder were dropped, the second sync would report 150,000 steps
    // the server has never heard of -- or, worse, the same steps twice.
    let ledger = anchoredAt(0, 400_000);
    let reported = 0;

    const firstBatch = batchToSend(ledger);
    reported += firstBatch;
    ledger = commitSync(ledger, firstBatch, 1_000);

    const secondBatch = batchToSend(ledger);
    reported += secondBatch;
    ledger = commitSync(ledger, secondBatch, 2_000);

    expect(reported).toBe(400_000);
    expect(stepsAwaitingSync(ledger)).toBe(0);
    expect(hasStepsToSync(ledger)).toBe(false);
  });
});

describe('surviving the app being killed mid-walk', () => {
  it('does not re-report a walk already synced', () => {
    // Android kills backgrounded apps freely. On restart the persisted ledger
    // has 5,500 steps still awaiting a sync and the pedometer has moved from
    // 25,500 to 30,000. The 4,500 new steps are added, and the 5,500 that were
    // never synced are still owed -- the 25,500 before them are not, and
    // re-anchoring on the reading instead of accumulating would have thrown
    // those 5,500 away.
    const persisted = { lastReading: 25_500, pendingSteps: 5_500, lastSyncedAt: 1_000 };
    expect(stepsAwaitingSync(observe(persisted, 30_000))).toBe(10_000);
  });

  it('keeps unsynced steps when the sync fails', () => {
    // The one way to lose a walk permanently: committing before the server has
    // answered. Nothing is committed on a failure, so the next attempt resends
    // the same walk.
    const ledger = anchoredAt(0, 1_200);
    expect(stepsAwaitingSync(ledger)).toBe(1_200);
  });
});

describe('manaWorth', () => {
  it('mirrors the server: a completed block of 1000 steps is worth 20', () => {
    expect(manaWorth(1_000)).toBe(20);
    expect(manaWorth(2_000)).toBe(40);
  });

  it('does not round a partial block up', () => {
    // 1,500 steps carries 1.5 blocks. The server credits floor(1.5) * 20 = 20
    // and banks the half, so showing 40 here would promise mana that does not
    // arrive.
    expect(manaWorth(1_500)).toBe(20);
    expect(manaWorth(1_999)).toBe(20);
    expect(manaWorth(999)).toBe(0);
  });

  it('never values a negative count', () => {
    expect(manaWorth(-100)).toBe(0);
  });
});
