import { describe, expect, it } from 'vitest';

import { ApiError, describeErrorBody, parseRetryAfter } from '../../src/api/errors';
import { apiErrorFromResponse } from '../../src/api/errors';

describe('parseRetryAfter', () => {
  it('reads a delay in seconds', () => {
    expect(parseRetryAfter('30')).toBe(30);
    expect(parseRetryAfter('0')).toBe(0);
  });

  it('rounds a fractional delay up', () => {
    // Rounding down would retry before the window has actually reopened, and
    // the second attempt would be limited again.
    expect(parseRetryAfter('1.2')).toBe(2);
  });

  it('reads an HTTP date, as a proxy may rewrite it to', () => {
    const future = new Date(Date.now() + 45_000).toUTCString();
    const parsed = parseRetryAfter(future);
    expect(parsed).not.toBeNull();
    expect(parsed).toBeGreaterThan(40);
    expect(parsed).toBeLessThanOrEqual(45);
  });

  it('clamps a past date to zero rather than going negative', () => {
    const past = new Date(Date.now() - 60_000).toUTCString();
    expect(parseRetryAfter(past)).toBe(0);
  });

  it('returns null for junk, never NaN', () => {
    // NaN leaking into a setTimeout delay is the bug this guards.
    expect(parseRetryAfter('soon')).toBeNull();
    expect(parseRetryAfter(null)).toBeNull();
  });
});

describe('describeErrorBody', () => {
  it('uses a string detail', () => {
    expect(describeErrorBody({ detail: 'Tile is not adjacent to your territory' })).toBe(
      'Tile is not adjacent to your territory',
    );
  });

  it('flattens FastAPI validation objects', () => {
    const body = {
      detail: [
        { loc: ['body', 'steps_delta'], msg: 'ensure this value is less than or equal to 10000' },
      ],
    };
    const message = describeErrorBody(body);
    // The field name is worth keeping: it is the only clue as to which input is
    // wrong. The Pydantic phrasing is not, and the player should not read the
    // schema's own wording back at them.
    expect(message).toContain('steps_delta');
    expect(message).toBe('steps_delta must be at most 10000');
  });

  it('translates the validation phrasings that actually occur', () => {
    const one = (msg: string) => describeErrorBody({ detail: [{ loc: ['body', 'x'], msg }] });
    expect(one('ensure this value is greater than or equal to 0')).toBe('x must be at least 0');
    expect(one('String should have at least 8 characters')).toBe('x must be at least 8 characters');
    expect(one('Field required')).toBe('x is required');
    expect(one('value is not a valid email address')).toBe('x is not a valid email address');
  });

  it('strips the prefix a custom validator leaves behind', () => {
    // The backend's own validators raise ValueError, which Pydantic forwards
    // with this prefix attached.
    const message = describeErrorBody({
      detail: [
        {
          loc: ['body', 'password'],
          msg: 'Value error, password must be at most 72 bytes when UTF-8 encoded',
        },
      ],
    });
    expect(message).toBe('password must be at most 72 bytes when UTF-8 encoded');
  });

  it('joins several validation problems', () => {
    const message = describeErrorBody({
      detail: [
        { loc: ['body', 'a'], msg: 'Field required' },
        { loc: ['body', 'b'], msg: 'Field required' },
      ],
    });
    expect(message).toBe('a is required; b is required');
  });

  it('survives a body that is not JSON at all', () => {
    // A 502 from a proxy is HTML. Returning undefined here would show the
    // player "undefined".
    expect(describeErrorBody(null)).toMatch(/unexpected/i);
    expect(describeErrorBody('<html>502</html>')).toMatch(/unexpected/i);
    expect(describeErrorBody({})).toMatch(/rejected/i);
  });
});

describe('apiErrorFromResponse', () => {
  const headers = (map: Record<string, string>) => ({
    get: (name: string) => map[name] ?? null,
  });

  it('carries rate limit headers off a 429', () => {
    const error = apiErrorFromResponse(
      429,
      { detail: 'Rate limit exceeded' },
      headers({ 'Retry-After': '12', 'X-RateLimit-Limit': '60', 'X-RateLimit-Remaining': '0' }),
    );
    expect(error.isRateLimited).toBe(true);
    expect(error.retryAfterSeconds).toBe(12);
    expect(error.rateLimitLimit).toBe(60);
    expect(error.rateLimitRemaining).toBe(0);
    expect(error.userMessage).toContain('12s');
  });

  it('reports a 429 without the headers as rate limited but not timed', () => {
    // A 429 is the limiter working. The client must not treat it as a failure
    // to report, only as a reason to wait.
    const error = apiErrorFromResponse(429, { detail: 'Rate limit exceeded' }, headers({}));
    expect(error.isRateLimited).toBe(true);
    expect(error.retryAfterSeconds).toBeNull();
    expect(error.userMessage).toMatch(/slow down/i);
  });

  it('classifies the statuses that deserve different words on screen', () => {
    const call = (status: number) => apiErrorFromResponse(status, { detail: 'x' }, headers({}));
    expect(call(401).userMessage).toMatch(/sign in/i);
    expect(call(402).isGameRule).toBe(true);
    expect(call(409).isGameRule).toBe(true);
    expect(call(422).isGameRule).toBe(true);
    // A 500 is ours, not the player's, so it gets a generic apology rather
    // than a leaked server string.
    expect(call(500).userMessage).toBe('Something went wrong. Please try again.');
  });

  it('marks an unparsable rate limit header as absent', () => {
    const error = apiErrorFromResponse(429, { detail: 'x' }, headers({ 'X-RateLimit-Limit': 'lots' }));
    expect(error.rateLimitLimit).toBeNull();
  });

  it('treats a missing status as offline', () => {
    const error = new ApiError({ status: null, detail: 'Cannot reach the server.' });
    expect(error.isOffline).toBe(true);
    expect(error.isAuthFailure).toBe(false);
    expect(error.isRateLimited).toBe(false);
    expect(error.userMessage).toMatch(/connection/i);
  });
});
