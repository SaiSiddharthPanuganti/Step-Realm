import { describe, expect, it } from 'vitest';

import { resolveBaseUrl } from '../../src/api/config';

describe('resolveBaseUrl', () => {
  it('uses 10.0.2.2 for the Android emulator', () => {
    // The single most common way a React Native app fails to reach a local
    // backend: localhost inside the emulator is the emulated device, so the
    // request goes nowhere and looks exactly like a dead server.
    expect(resolveBaseUrl({ platform: 'android' })).toBe('http://10.0.2.2:8000');
  });

  it('uses localhost for the iOS simulator', () => {
    expect(resolveBaseUrl({ platform: 'ios' })).toBe('http://localhost:8000');
  });

  it('follows the dev server address on a real device', () => {
    // A phone cannot reach the laptop's localhost, so it uses the LAN address
    // the bundle was already downloaded from -- the one host proven to work.
    expect(
      resolveBaseUrl({ platform: 'android', hostUri: '192.168.1.20:8081' }),
    ).toBe('http://192.168.1.20:8000');
    expect(
      resolveBaseUrl({ platform: 'ios', hostUri: '10.0.0.5:8081' }),
    ).toBe('http://10.0.0.5:8000');
  });

  it('remaps a loopback dev server to the emulator alias on Android only', () => {
    expect(
      resolveBaseUrl({ platform: 'android', hostUri: 'localhost:8081' }),
    ).toBe('http://10.0.2.2:8000');
    expect(
      resolveBaseUrl({ platform: 'ios', hostUri: '127.0.0.1:8081' }),
    ).toBe('http://localhost:8000');
  });

  it('lets an explicit override win', () => {
    // Needed on cellular or a VPN, where the LAN address is unreachable, and
    // for a build pointing at a deployed host.
    expect(
      resolveBaseUrl({
        platform: 'android',
        hostUri: '192.168.1.20:8081',
        envUrl: 'https://api.steprealm.app',
      }),
    ).toBe('https://api.steprealm.app');
  });

  it('strips a trailing slash so paths do not double up', () => {
    // `${base}//me` gets a 307 from FastAPI, and a redirect on a POST drops the
    // body, so a login would look like it worked and create no account.
    expect(resolveBaseUrl({ platform: 'ios', envUrl: 'https://api.test///' })).toBe(
      'https://api.test',
    );
  });

  it('rejects a URL with no scheme', () => {
    expect(() => resolveBaseUrl({ platform: 'ios', envUrl: 'api.steprealm.app' })).toThrow(
      /http/,
    );
  });

  it('honours a custom port', () => {
    expect(resolveBaseUrl({ platform: 'ios', port: 9000 })).toBe('http://localhost:9000');
    expect(
      resolveBaseUrl({ platform: 'android', hostUri: '192.168.1.20:8081', port: 9000 }),
    ).toBe('http://192.168.1.20:9000');
  });

  it('ignores a blank override', () => {
    // An unset EXPO_PUBLIC_API_URL arrives as an empty string, not undefined,
    // and treating it as a URL would throw at startup on every fresh clone.
    expect(resolveBaseUrl({ platform: 'ios', envUrl: '   ' })).toBe('http://localhost:8000');
  });
});
