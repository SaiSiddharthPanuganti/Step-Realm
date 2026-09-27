/**
 * Where the client looks for the API.
 *
 * This is a pure module with no React Native imports so the rules below can be
 * unit tested. `src/api/runtime.ts` is the thin wrapper that feeds it real
 * values.
 */

/** Loopback, as seen from inside the Android emulator. */
const ANDROID_EMULATOR_HOST = '10.0.2.2';

/** Dev server port for the backend (`uvicorn app.main:app --reload`). */
export const DEFAULT_API_PORT = 8000;

export type Platform = 'ios' | 'android' | 'web';

export interface ResolveOptions {
  /**
   * An explicit override, normally `EXPO_PUBLIC_API_URL`.
   *
   * Needed for two cases this function cannot infer: a physical device on
   * cellular or a VPN, where the LAN address is not reachable; and a deployed
   * build pointing at a real host.
   */
  envUrl?: string | null;
  platform: Platform;
  /**
   * `expoConfig.hostUri` -- `192.168.1.20:8081`, the address the developer
   * machine is already reachable at, since that is what the phone used to
   * download the JS bundle.
   *
   * Preferred over a guessed address because it is the one host already proven
   * to work for this device, and it changes with the network: a laptop on
   * Wi-Fi and one on a hotspot have different addresses, and hardcoding either
   * means the app silently works in the developer's kitchen and nowhere else.
   */
  hostUri?: string | null;
  port?: number;
}

/**
 * Strip a trailing slash and reject anything that is not http(s).
 *
 * A trailing slash turns `${base}/me` into `//me` on a real server, which
 * FastAPI answers with a redirect -- and a redirect on a POST drops the body,
 * so a login would appear to succeed and then silently fail to create an
 * account. Cheap to prevent, quietly awful to debug.
 */
function normaliseBaseUrl(raw: string): string {
  const trimmed = raw.trim().replace(/\/+$/, '');
  if (!/^https?:\/\//i.test(trimmed)) {
    throw new Error(
      `API base URL must start with http:// or https://, got: ${raw}`,
    );
  }
  return trimmed;
}

/** Keep only the host from a `host:port` pair, dropping the port. */
function hostOf(hostUri: string): string | null {
  const withoutScheme = hostUri.replace(/^[a-z]+:\/\//i, '');
  const host = withoutScheme.split(':')[0]?.trim();
  return host ? host : null;
}

/**
 * Decide the API base URL for this platform.
 *
 * The emulator is the reason this is not a constant. Inside the Android
 * emulator `localhost` is the emulated device, not the developer's machine, so
 * an app configured with `http://localhost:8000` connects to nothing and fails
 * with a connection error that looks exactly like the server being down. The
 * emulator reaches the host through 10.0.2.2, which is the one address that
 * always means "the machine running this emulator" regardless of its LAN IP.
 */
export function resolveBaseUrl(options: ResolveOptions): string {
  const port = options.port ?? DEFAULT_API_PORT;

  if (options.envUrl && options.envUrl.trim() !== '') {
    return normaliseBaseUrl(options.envUrl);
  }

  const host = options.hostUri ? hostOf(options.hostUri) : null;

  if (host) {
    // A dev server on a public host or a tunnel is reachable as-is.
    if (host !== 'localhost' && host !== '127.0.0.1') {
      return `http://${host}:${port}`;
    }
    // hostUri pointed at loopback: fine for iOS simulator and web, wrong for
    // the Android emulator.
    if (options.platform !== 'android') {
      return `http://localhost:${port}`;
    }
    return `http://${ANDROID_EMULATOR_HOST}:${port}`;
  }

  // No dev server address at all, so fall back to whatever works here.
  if (options.platform === 'android') {
    return `http://${ANDROID_EMULATOR_HOST}:${port}`;
  }
  return `http://localhost:${port}`;
}
