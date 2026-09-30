/**
 * The HTTP layer. One place that knows how to talk to the backend.
 *
 * Deliberately free of React Native imports. `expo-secure-store` and the
 * constants module are wired in from the outside via `TokenStore` and
 * `resolveBaseUrl`, which keeps this file -- and every module above it -- runnable
 * under plain Node, so the contract tests can drive a real server without a
 * simulator or a native module stub.
 */

import { ApiError, apiErrorFromResponse, describeErrorBody } from './errors';
import type { ApiStatus } from './types';

/**
 * Where the bearer token lives.
 *
 * An interface rather than a direct import of `expo-secure-store` for two
 * reasons: it keeps this module testable, and it keeps the choice of storage
 * reversible. SecureStore is the right default -- the token is a bearer
 * credential with no expiry of its own, and AsyncStorage is plain text in the
 * app's sandbox directory, readable from a rooted device or an adb backup.
 */
export interface TokenStore {
  get(): Promise<string | null>;
  set(token: string): Promise<void>;
  clear(): Promise<void>;
}

/**
 * Asked for the token before every authenticated request.
 *
 * Async because secure storage is, and doing this synchronously would mean
 * holding the JWT in memory for the life of the process just to avoid one
 * await. A null token means "send unauthenticated" -- GET /world is public, and
 * making that explicit means the client can browse the map before signing up.
 */
export type TokenProvider = () => Promise<string | null>;

export interface ApiClientOptions {
  baseUrl: string;
  tokenStore?: TokenStore;
  /**
   * Called when the server rejects the stored token, so the app can sign out.
   *
   * Fired only for 401s on requests that actually sent a token. A 401 from
   * `/auth/login` means the password was wrong, and signing the player out
   * because they mistyped it would discard the form they are filling in.
   */
  onUnauthorized?: () => void;
  /** Per-request timeout. The server is on a LAN and should be fast. */
  timeoutMs?: number;
  /** Injected for tests so a stub can stand in for global `fetch`. */
  fetchImpl?: typeof fetch;
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH';
  body?: unknown;
  /** Skip the Authorization header, even when a token is available. */
  anonymous?: boolean;
  query?: Record<string, string | number | boolean | undefined | null>;
  signal?: AbortSignal;
}

const DEFAULT_TIMEOUT_MS = 15_000;

/** Drop null and undefined so optional query parameters can be passed inline. */
function buildQuery(
  query: RequestOptions['query'],
): string {
  if (!query) {
    return '';
  }
  const parts: string[] = [];
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) {
      continue;
    }
    parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
  }
  return parts.length > 0 ? `?${parts.join('&')}` : '';
}

/**
 * Read a body without assuming it is JSON.
 *
 * A 502 from a proxy is HTML, and a 204 is empty. Calling `json()` on either
 * throws a `SyntaxError` that would replace a real HTTP failure with a
 * confusing parse failure.
 */
async function readBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text === '') {
    return null;
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return { detail: text.slice(0, 200) };
  }
}

export class ApiClient {
  readonly baseUrl: string;
  private readonly tokenStore: TokenStore | undefined;
  private readonly onUnauthorized: (() => void) | undefined;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;

  constructor(options: ApiClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, '');
    this.tokenStore = options.tokenStore;
    this.onUnauthorized = options.onUnauthorized;
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch;
  }

  /** The token to send, or null for an anonymous request. */
  private async headerFor(anonymous: boolean): Promise<Record<string, string>> {
    if (anonymous || !this.tokenStore) {
      return {};
    }
    const token = await this.tokenStore.get();
    return token ? { Authorization: `Bearer ${token}` } : {};
  }

  /**
   * Make one request.
   *
   * Never resolves with a non-2xx: it throws an `ApiError`. Nothing above this
   * layer has to remember to check `response.ok`, which is the failure mode
   * that produces a client that reports success while displaying stale data.
   */
  async request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const { method = 'GET', body, anonymous = false } = options;
    const url = `${this.baseUrl}${path}${buildQuery(options.query)}`;

    // The caller's signal and the timeout have to be combined. Passing only
    // the timeout would make a screen unmount mid-request impossible to
    // cancel; passing only the caller's signal would let a hung request pin a
    // spinner forever.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    const onExternalAbort = () => controller.abort();
    options.signal?.addEventListener('abort', onExternalAbort);

    let headers: Record<string, string>;
    try {
      headers = {
        Accept: 'application/json',
        ...(await this.headerFor(anonymous)),
      };
      if (body !== undefined) {
        headers['Content-Type'] = 'application/json';
      }
    } catch (cause) {
      // Secure storage itself failing is an offline condition from the app's
      // point of view, and the player can act on it.
      clearTimeout(timer);
      options.signal?.removeEventListener('abort', onExternalAbort);
      throw new ApiError({
        status: null,
        detail: 'Could not read stored credentials.',
        cause,
      });
    }

    let response: Response;
    try {
      response = await this.fetchImpl(url, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
    } catch (cause) {
      if (controller.signal.aborted && !options.signal?.aborted) {
        throw new ApiError({
          status: null,
          detail: `The server did not respond within ${Math.round(this.timeoutMs / 1000)}s.`,
          cause,
        });
      }
      if (controller.signal.aborted) {
        throw new ApiError({
          status: null,
          detail: 'Request cancelled.',
          cause,
        });
      }
      throw new ApiError({
        status: null,
        detail: 'Cannot reach the server.',
        cause,
      });
    } finally {
      clearTimeout(timer);
      options.signal?.removeEventListener('abort', onExternalAbort);
    }

    if (response.status === 401 && !anonymous) {
      this.onUnauthorized?.();
    }

    if (!response.ok) {
      const parsed = await readBody(response);
      throw apiErrorFromResponse(
        response.status,
        parsed,
        response.headers,
        parsed,
      );
    }

    if (response.status === 204) {
      return undefined as T;
    }
    const payload = await readBody(response);
    if (payload === null) {
      throw new ApiError({
        status: null,
        detail: 'The server returned an empty response where data was expected.',
      });
    }
    return payload as T;
  }

  get<T>(path: string, query?: RequestOptions['query']): Promise<T> {
    return this.request<T>(path, { method: 'GET', query });
  }

  post<T>(path: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: 'POST', body });
  }

  patch<T>(path: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: 'PATCH', body });
  }
}

export { ApiError, describeErrorBody };
export type { ApiStatus };
