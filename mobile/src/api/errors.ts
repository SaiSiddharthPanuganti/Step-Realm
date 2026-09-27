/**
 * A failed API call, in a form the UI can branch on.
 *
 * The alternative -- letting `fetch` errors and HTTP error statuses be
 * different things -- forces every caller to remember to check both, and the
 * one caller that forgets shows the player "Network request failed" for a
 * perfectly ordinary empty mana bar. Everything that can go wrong on a request
 * arrives here as an `ApiError`, with a `status` that distinguishes the cases
 * that deserve different words on screen.
 */

import type { ApiStatus } from './types';

/**
 * A well-formed error body from FastAPI.
 *
 * `detail` is normally a string, but FastAPI also uses it for a list of
 * validation objects on a 422, so both shapes have to be handled.
 */
interface ErrorBody {
  detail?: string | { msg?: string; loc?: (string | number)[] }[];
}

export class ApiError extends Error {
  /** `null` when the request never reached the server (offline, DNS, refused). */
  readonly status: ApiStatus | null;
  readonly detail: string;
  /** Seconds to wait, parsed from `Retry-After`. Only set on a 429. */
  readonly retryAfterSeconds: number | null;
  readonly rateLimitLimit: number | null;
  readonly rateLimitRemaining: number | null;
  readonly cause: unknown;

  constructor(init: {
    status: ApiStatus | null;
    detail: string;
    retryAfterSeconds?: number | null;
    rateLimitLimit?: number | null;
    rateLimitRemaining?: number | null;
    cause?: unknown;
  }) {
    super(init.detail);
    this.name = 'ApiError';
    this.status = init.status;
    this.detail = init.detail;
    this.retryAfterSeconds = init.retryAfterSeconds ?? null;
    this.rateLimitLimit = init.rateLimitLimit ?? null;
    this.rateLimitRemaining = init.rateLimitRemaining ?? null;
    this.cause = init.cause;
  }

  /** True when the server never answered, as opposed to answering "no". */
  get isOffline(): boolean {
    return this.status === null;
  }

  /** The stored token was rejected. The app should sign out and re-prompt. */
  get isAuthFailure(): boolean {
    return this.status === 401;
  }

  /**
   * The server is rate limiting this client.
   *
   * Worth its own flag because the correct response is to wait, not to
   * retry immediately, and not to tell the player anything went wrong: a 429
   * is the limiter working, which is the desired behaviour.
   */
  get isRateLimited(): boolean {
    return this.status === 429;
  }

  /**
   * A rule was broken that the player can act on: not enough mana, not
   * adjacent, too far from home. Distinct from a 422 validation error because
   * the message is a gameplay explanation, not a client bug.
   */
  get isGameRule(): boolean {
    return this.status === 402 || this.status === 409 || this.status === 422;
  }

  /**
   * A message fit to show a player, with the internal detail kept out of it.
   *
   * `status === null` is handled explicitly so a dropped connection reads as
   * "check your connection" rather than surfacing a raw fetch message like
   * "Network request failed" from whatever platform produced it.
   */
  get userMessage(): string {
    if (this.status === null) {
      return 'Cannot reach the server. Check your connection and try again.';
    }
    if (this.status === 401) {
      return 'Your session expired. Please sign in again.';
    }
    if (this.status === 429) {
      return this.retryAfterSeconds !== null
        ? `Slow down a moment -- try again in ${this.retryAfterSeconds}s.`
        : 'Slow down a moment and try again shortly.';
    }
    if (this.status === 402) {
      return this.detail;
    }
    if (this.status === 409) {
      return this.detail;
    }
    if (this.status === 422) {
      return this.detail;
    }
    return 'Something went wrong. Please try again.';
  }
}

/**
 * Rewrite Pydantic's validation phrasing into something a person would say.
 *
 * Only the two shapes that actually occur here, rather than a lookup table that
 * would rot: the range/comparison phrasings and the `Value error, ` prefix that
 * Pydantic puts in front of a custom validator's message. Leaving them as-is
 * means the player reads "ensure this value is less than or equal to 10000",
 * which describes the schema to the developer, not the problem to the player.
 */
function humanise(message: string): string {
  return message
    .replace(/^Value error,\s*/, '')
    .replace(/^ensure this value is less than or equal to (.+)$/, 'must be at most $1')
    .replace(/^ensure this value is greater than or equal to (.+)$/, 'must be at least $1')
    .replace(/^ensure this value is less than (.+)$/, 'must be under $1')
    .replace(/^ensure this value is greater than (.+)$/, 'must be over $1')
    .replace(/^ensure this value is between (.+) and (.+)$/, 'must be between $1 and $2')
    .replace(/^value is not a valid email address$/, 'is not a valid email address')
    .replace(/^String should have at least (\d+) characters?$/, 'must be at least $1 characters')
    .replace(/^String should have at most (\d+) characters?$/, 'must be at most $1 characters')
    .replace(/^Field required$/, 'is required');
}

/**
 * Pull a player-legible message out of a FastAPI error body.
 *
 * A 422 from request validation arrives as `detail: [{loc, msg, ...}]`. The
 * field name is kept even though the client already knows which field it sent,
 * because it is the only reliable clue when a request is assembled in more
 * than one place -- and it names the offending input rather than the rule.
 */
export function describeErrorBody(body: unknown): string {
  if (body === null || typeof body !== 'object') {
    return 'Unexpected response from the server.';
  }
  const { detail } = body as ErrorBody;
  if (typeof detail === 'string') {
    return detail;
  }
  if (Array.isArray(detail) && detail.length > 0) {
    const parts = detail.map((entry) => {
      const field = entry.loc?.filter((x) => x !== 'body').join('.') ?? '';
      const msg = humanise(entry.msg ?? 'is invalid');
      if (!field) {
        return msg;
      }
      // A validator's own message usually already names the field it is about
      // ("password must be at most 72 bytes"). Prefixing the path there yields
      // "password password must be at most 72 bytes", which reads like a bug to
      // the player because it is one.
      const alreadyNamed = field
        .split('.')
        .every((part) => msg.toLowerCase().startsWith(`${part.toLowerCase()} `) || msg.toLowerCase() === part.toLowerCase());
      return alreadyNamed ? msg : `${field} ${msg}`;
    });
    return parts.join('; ');
  }
  return 'The request was rejected.';
}

/**
 * Parse `Retry-After`, which may be a delay in seconds or an HTTP date.
 *
 * The server sends seconds, but a proxy in front of it may rewrite the header
 * into a date, so both are handled. A date that is not a number of seconds
 * resolves to `null` rather than `NaN`, so a caller doing arithmetic on this
 * cannot propagate a NaN into a retry delay.
 */
export function parseRetryAfter(header: string | null): number | null {
  if (header === null) {
    return null;
  }
  const asSeconds = Number(header);
  if (Number.isFinite(asSeconds) && asSeconds >= 0) {
    return Math.ceil(asSeconds);
  }
  const asDate = Date.parse(header);
  if (Number.isNaN(asDate)) {
    return null;
  }
  return Math.max(0, Math.ceil((asDate - Date.now()) / 1000));
}

/** Parse a header that is either an integer or absent. */
function parseIntHeader(header: string | null): number | null {
  if (header === null) {
    return null;
  }
  const parsed = Number(header);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * Build an `ApiError` from a non-2xx response.
 *
 * `body` is passed in already parsed because a 500 from a proxy can be HTML,
 * which is not JSON, and `response.json()` on it throws -- turning a rate
 * limit into an unhandled parse error.
 */
export function apiErrorFromResponse(
  status: number,
  body: unknown,
  headers: { get(name: string): string | null },
  cause?: unknown,
): ApiError {
  return new ApiError({
    status: status as ApiStatus,
    detail: describeErrorBody(body),
    retryAfterSeconds: parseRetryAfter(headers.get('Retry-After')),
    rateLimitLimit: parseIntHeader(headers.get('X-RateLimit-Limit')),
    rateLimitRemaining: parseIntHeader(headers.get('X-RateLimit-Remaining')),
    cause,
  });
}
