import { describe, expect, it } from 'vitest';

import { worldToLonLat } from '../../src/map/geojson';
import {
  commitReports,
  driftFilter,
  emptyFallbackRun,
  emptyLedger,
  emptyStillness,
  fallbackMoving,
  isMovingFix,
  observeSegment,
  paceMeters,
  pendingFor,
  reportedFor,
  toReports,
  WALK_SPEED_FLOOR_METERS_PER_SECOND,
  MAX_TRACK_SPEED_METERS_PER_SECOND,
  type WalkLedger,
} from '../../src/walk/accumulate';

const SIZE = 40;

/** A WGS84 point displaced `dx`/`dy` plane-metres from the origin. */
function at(dxMeters: number, dyMeters: number): { lat: number; lon: number } {
  const [lon, lat] = worldToLonLat(dxMeters, dyMeters);
  return { lat, lon };
}

describe('isMovingFix', () => {
  it('treats a still phone as stationary however far its fix drifted', () => {
    // A standing phone reports ~0.3 m/s of doppler wander. That is below the
    // walking floor, and it stays stationary even though the fix point can
    // rock several metres -- position never enters this decision.
    expect(isMovingFix(0.3)).toBe(false);
    expect(isMovingFix(0)).toBe(false);
    // The floor sits at a brisk idle: borderline 0.8 m/s readings on a still
    // chip are not movement either. (1.0 m/s and up is.)
    expect(isMovingFix(0.8)).toBe(false);
  });

  it('accepts a walking pace', () => {
    expect(isMovingFix(WALK_SPEED_FLOOR_METERS_PER_SECOND)).toBe(true);
    expect(isMovingFix(1.4)).toBe(true);
    expect(isMovingFix(3.2)).toBe(true);
  });

  it('refuses a pace the server would reject', () => {
    expect(isMovingFix(MAX_TRACK_SPEED_METERS_PER_SECOND + 0.1)).toBe(false);
    expect(isMovingFix(44)).toBe(false);
  });

  it('refuses a chip that said nothing', () => {
    expect(isMovingFix(null)).toBe(false);
    expect(isMovingFix(undefined)).toBe(false);
    expect(isMovingFix(Number.NaN)).toBe(false);
  });
});

describe('paceMeters', () => {
  it('integrates speed over the step time', () => {
    expect(paceMeters(1.3, 2)).toBeCloseTo(2.6, 9);
  });

  it('caps the credited window to what was actually measured', () => {
    // A 1.3 m/s step with a 10 s gap claims 1.3 * 3 = 3.9 m, not 13 m: the
    // seven unmeasured seconds might have been a dead radio, not a walk.
    expect(paceMeters(1.3, 10)).toBeCloseTo(1.3 * 3, 9);
  });

  it('refuses timing that cannot be a step', () => {
    expect(paceMeters(1.3, 0)).toBeNull();
    expect(paceMeters(1.3, -1)).toBeNull();
    expect(paceMeters(1.3, Number.NaN)).toBeNull();
  });

  it('refuses a speed that is not a speed', () => {
    expect(paceMeters(0, 2)).toBeNull();
    expect(paceMeters(-1, 2)).toBeNull();
    expect(paceMeters(Number.NaN, 2)).toBeNull();
  });
});

describe('fallbackMoving (chip reports no speed)', () => {
  it('does not credit a single long hop: drift pokes out and back', () => {
    const first = fallbackMoving(null, at(0, 0), at(10, 0));
    expect(first.moving).toBe(false);
    expect(first.meters).toBe(0);
  });

  it('credits once two hops continue in the same direction', () => {
    const first = fallbackMoving(null, at(0, 0), at(10, 0));
    const second = fallbackMoving(first.nextState, at(10, 0), at(20, 0));
    expect(first.moving).toBe(false);
    expect(second.moving).toBe(true);
    expect(second.meters).toBeGreaterThan(9);
  });

  it('breaks the run, and the metres, when a hop doubles back', () => {
    // East then sharply west is the fingerprint of a still phone's random
    // walk: nothing about the first hop was real, so it earns nothing either.
    const east = fallbackMoving(null, at(0, 0), at(10, 0));
    const west = fallbackMoving(east.nextState, at(10, 0), at(0, 0));
    expect(west.moving).toBe(false);
    expect(west.meters).toBe(0);
    expect(west.nextState.streak).toBe(1);
  });

  it('ignores hops smaller than the jitter floor', () => {
    const verdict = fallbackMoving(null, at(0, 0), at(1, 0));
    expect(verdict.moving).toBe(false);
    expect(verdict.nextState.streak).toBe(0);
  });
});

describe('observeSegment', () => {
  it('credits metres to the hex that contained the start', () => {
    const start = at(60, 0);
    const result = observeSegment(emptyLedger(), start, 2.6, SIZE);
    // 60 m east of the origin is inside the hex whose centre is at (1,0) in
    // the test grid; the metres belong there, wherever they were earned.
    expect(Object.keys(result)).toHaveLength(1);
    const [bucket] = Object.values(result);
    expect(bucket.q).toBe(1);
    expect(bucket.r).toBe(0);
    expect(bucket.pending).toBeCloseTo(2.6, 9);
    expect(bucket.lat).toBe(start.lat);
    expect(bucket.lon).toBe(start.lon);
  });

  it('ignores a step that earned nothing', () => {
    expect(observeSegment(emptyLedger(), at(0, 0), 0, SIZE)).toEqual({});
    expect(observeSegment(emptyLedger(), at(0, 0), Number.NaN, SIZE)).toEqual({});
    expect(observeSegment(emptyLedger(), at(0, 0), -1, SIZE)).toEqual({});
  });

  it('returns a fresh ledger rather than mutating the input', () => {
    const start = emptyLedger();
    const result = observeSegment(start, at(0, 0), 4, SIZE);
    expect(result).not.toBe(start);
    expect(Object.keys(start)).toHaveLength(0);
  });

  it('accumulates several steps into the same bucket', () => {
    let ledger: WalkLedger = emptyLedger();
    const a = at(0, 0);
    ledger = observeSegment(ledger, a, 1.2, SIZE);
    ledger = observeSegment(ledger, a, 2.4, SIZE);
    expect(pendingFor(ledger, 0, 0)).toBeCloseTo(3.6, 9);
    const [bucket] = Object.values(ledger);
    expect(bucket.lat).toBe(a.lat);
    expect(bucket.lon).toBe(a.lon);
  });
});

describe('reports', () => {
  it('reports nothing from an empty or fully-committed ledger', () => {
    expect(toReports(emptyLedger())).toHaveLength(0);
    // A bucket whose pending is below the floor -- possible by hand, though a
    // tracked step always exceeds it in practice.
    const thin: WalkLedger = {
      '0,0': { q: 0, r: 0, lat: 0, lon: 0, pending: 0.3, reported: 0 },
    };
    expect(toReports(thin)).toHaveLength(0);
  });

  it('reports tracked buckets past the floor, rounded to a decimal', () => {
    const full: WalkLedger = {
      '0,0': { q: 0, r: 0, lat: 0, lon: 0, pending: 9.466, reported: 0 },
    };
    const reports = toReports(full);
    expect(reports).toHaveLength(1);
    const [report] = reports;
    expect(report.q).toBe(0);
    expect(report.r).toBe(0);
    expect(report.meters).toBeCloseTo(9.5, 9);
  });

  it('keeps two hexes as two reports', () => {
    const two: WalkLedger = {
      '0,0': { q: 0, r: 0, lat: 0, lon: 0, pending: 5, reported: 0 },
      '1,-1': { q: 1, r: -1, lat: 1, lon: 2, pending: 7, reported: 0 },
    };
    const reports = toReports(two);
    expect(reports).toHaveLength(2);
    expect(new Set(reports.map((report) => `${report.q},${report.r}`)).size).toBe(2);
  });

  it('moves accepted metres from pending into reported', () => {
    const full: WalkLedger = {
      '0,0': { q: 0, r: 0, lat: 0, lon: 0, pending: 9.466, reported: 0 },
    };
    const next = commitReports(full, [{ q: 0, r: 0 }]);
    expect(pendingFor(next, 0, 0)).toBe(0);
    // The amount archived is what was actually sent and accepted -- the
    // rounded total -- so the client never believes it holds metres the
    // server did not record.
    expect(reportedFor(next, 0, 0)).toBeCloseTo(9.5, 9);
  });

  it('does nothing when asked to commit an unknown hex', () => {
    const ledger = emptyLedger();
    expect(commitReports(ledger, [{ q: 5, r: 5 }])).toBe(ledger);
  });
});

describe('emptyFallbackRun', () => {
  it('starts with no direction and no streak', () => {
    expect(emptyFallbackRun()).toEqual({ streak: 0, prevBearingDeg: null });
  });
});

describe('driftFilter', () => {
  it('releases the very first fix, and treats a moving fix as movement', () => {
    const origin = at(0, 0);
    const first = driftFilter({
      speed: 0.3,
      stillness: emptyStillness(),
      fix: origin,
      released: null,
    });
    expect(first.follow).toBe(true);
    expect(first.released).toEqual(origin);

    const moved = driftFilter({
      speed: 1.4,
      stillness: emptyStillness(),
      fix: at(5, 0),
      released: origin,
    });
    expect(moved.follow).toBe(true);
    expect(moved.released).toEqual(at(5, 0));
  });

  it('holds the marker through a still phone\'s fix-point wander', () => {
    const origin = at(0, 0);
    const stillness = emptyStillness();
    for (let step = 0; step < 4; step += 1) {
      const wander = at(step * 0.7, 0);
      const verdict = driftFilter({
        speed: 0.3,
        stillness,
        fix: wander,
        released: origin,
      });
      // Never moves the marker below the floor, however far the fix drifts.
      expect(verdict.follow).toBe(false);
      expect(verdict.released).toEqual(origin);
    }
  });

  it('settles once when a stop has proven itself, then holds', () => {
    const origin = at(0, 0);
    // The stop ends a walk at `origin`; the still fixes cluster ~1 m away.
    let stillness = emptyStillness();
    let released: { lat: number; lon: number } | null = origin;
    // Fixes 1..2: still deciding.
    for (let i = 1; i <= 2; i += 1) {
      const verdict = driftFilter({
        speed: 0.4,
        stillness,
        fix: at(0.8, 0),
        released,
      });
      stillness = verdict.stillness;
      released = verdict.released;
      expect(verdict.follow).toBe(false);
    }
    // Fix 3: the stop is proven; the marker settles on the still cluster once.
    const settled = driftFilter({
      speed: 0.4,
      stillness,
      fix: at(0.8, 0),
      released,
    });
    expect(settled.follow).toBe(true);
    expect(settled.released).toEqual(at(0.8, 0));
    // And from then on, further wander holds the settled position.
    const after = driftFilter({
      speed: 0.3,
      stillness: settled.stillness,
      fix: at(0.5, -0.6),
      released: settled.released,
    });
    expect(after.follow).toBe(false);
    expect(after.released).toEqual(at(0.8, 0));
  });

  it('re-anchors once when the position truly escaped a silent chip', () => {
    const origin = at(0, 0);
    const escaped = driftFilter({
      speed: 0.4,
      stillness: emptyStillness(),
      fix: at(9, 2),
      released: origin,
    });
    expect(escaped.follow).toBe(true);
    expect(escaped.released).toEqual(at(9, 2));
  });

  it('restarts a still spell when the wander leaves the cluster', () => {
    const origin = at(0, 0);
    let stillness = emptyStillness();
    let released: { lat: number; lon: number } | null = origin;
    // Anchored at (0.6, 0); the 4.0 m hop is 3.4 m away, far enough to leave
    // the 3 m still spell but not far enough (4 m+) to count as an escape.
    const runs = [at(0.6, 0), at(0.9, 0), at(4.0, 0)];
    for (const fix of runs) {
      const verdict = driftFilter({ speed: 0.3, stillness, fix, released });
      stillness = verdict.stillness;
      released = verdict.released;
    }
    // The spell restarted and the marker stayed put -- only an escape (4 m+)
    // would have moved it.
    expect(released).toEqual(origin);
    expect(stillness.streak).toBe(1);
  });
});

describe('driftFilter escape threshold', () => {
  it('holds at exactly the escape limit and re-anchors beyond it', () => {
    const origin = at(0, 0);
    const atLimit = driftFilter({
      speed: 0.3,
      stillness: emptyStillness(),
      fix: at(4, 0),
      released: origin,
    });
    expect(atLimit.follow).toBe(false);

    const beyond = driftFilter({
      speed: 0.3,
      stillness: emptyStillness(),
      fix: at(4.1, 0),
      released: origin,
    });
    expect(beyond.follow).toBe(true);
  });
});