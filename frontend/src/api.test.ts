/**
 * Tests for the typed data layer (`api.ts`).
 *
 * Everything runs against a stubbed global `fetch`: URL derivation from
 * the JupyterHub page configuration, the API-token authentication (the
 * single credential, cookies deliberately omitted), and the strict
 * shape validation of every service payload (a malformed or non-JSON
 * response must reject, never leak through as a trusted object).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { PageConfig } from '@jupyterlab/coreutils';

import {
  ServiceError,
  fetchServers,
  fetchStatus,
  loginUrl,
  postRevoke,
  postVerify,
  serviceBaseUrl
} from './api';

const TEST_TOKEN = 'test-hub-api-token';

const SERVERS_PAYLOAD = {
  servers: [
    {
      name: 'stable-discovery',
      label: 'Stable (discovery)',
      color_valid: '#2e7d32',
      color_invalid: '#c62828'
    },
    {
      name: 'rotating',
      label: 'Rotating',
      color_valid: '#2e7d32',
      color_invalid: '#c62828'
    }
  ]
};

const STATUS_PAYLOAD = {
  has_token: true,
  expires_at: 500.0,
  created_at: 100.0,
  updated_at: 200.0,
  scope: 'openid profile offline_access',
  refresh_kind: 'offline',
  refresh_expires_at: null
};

function jsonResponse(payload: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init
  });
}

function stubFetch(response: Response): ReturnType<typeof vi.fn> {
  const mock = vi.fn(async () => response);
  vi.stubGlobal('fetch', mock);
  return mock;
}

beforeEach(() => {
  PageConfig.setOption('hubPrefix', '');
  PageConfig.setOption('hubHost', '');
  PageConfig.setOption('token', TEST_TOKEN);
});

afterEach(() => {
  vi.unstubAllGlobals();
  PageConfig.setOption('hubPrefix', '');
  PageConfig.setOption('hubHost', '');
  PageConfig.setOption('token', '');
});

describe('serviceBaseUrl', () => {
  it('falls back to the root service path without a hub prefix', () => {
    expect(serviceBaseUrl()).toBe('/services/tessera/');
  });

  it('derives the service path from a root hub prefix', () => {
    PageConfig.setOption('hubPrefix', '/hub/');
    expect(serviceBaseUrl()).toBe('/services/tessera/');
  });

  it('keeps the deployment prefix of a non-root hub', () => {
    PageConfig.setOption('hubPrefix', '/jupyter/hub/');
    expect(serviceBaseUrl()).toBe('/jupyter/services/tessera/');
  });

  it('prepends the hub host when the hub lives on another origin', () => {
    PageConfig.setOption('hubHost', 'https://hub.example.com');
    PageConfig.setOption('hubPrefix', '/hub/');
    expect(serviceBaseUrl()).toBe('https://hub.example.com/services/tessera/');
  });
});

describe('loginUrl', () => {
  it('builds the login URL with the server name encoded', () => {
    expect(loginUrl('stable discovery')).toBe(
      '/services/tessera/login?server=stable%20discovery'
    );
  });
});

describe('fetchServers', () => {
  it('requests the servers route with the API token, without cookies', async () => {
    const mock = stubFetch(jsonResponse(SERVERS_PAYLOAD));
    const servers = await fetchServers();
    expect(servers).toEqual(SERVERS_PAYLOAD.servers);
    expect(mock).toHaveBeenCalledTimes(1);
    const [url, init] = mock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/services/tessera/servers');
    expect(init.credentials).toBe('omit');
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBe(`token ${TEST_TOKEN}`);
  });

  it('sends no Authorization header when the page has no token', async () => {
    PageConfig.setOption('token', '');
    const mock = stubFetch(jsonResponse(SERVERS_PAYLOAD));
    await fetchServers();
    const [, init] = mock.mock.calls[0] as [string, RequestInit];
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
  });

  it('reports the HTTP status of a denial (403 scopes diagnosis)', async () => {
    stubFetch(jsonResponse({ error: 'forbidden' }, { status: 403 }));
    await expect(fetchServers()).rejects.toMatchObject({ status: 403 });
  });

  it('forwards the abort signal to fetch', async () => {
    const mock = stubFetch(jsonResponse(SERVERS_PAYLOAD));
    const controller = new AbortController();
    await fetchServers(controller.signal);
    const [, init] = mock.mock.calls[0] as [string, RequestInit];
    expect(init.signal).toBe(controller.signal);
  });

  it('rejects a non-ok response', async () => {
    stubFetch(jsonResponse({ error: 'boom' }, { status: 500 }));
    await expect(fetchServers()).rejects.toThrow();
  });

  it('rejects a non-JSON response (login or confirmation page)', async () => {
    stubFetch(
      new Response('<html>sign in</html>', {
        status: 200,
        headers: { 'Content-Type': 'text/html' }
      })
    );
    await expect(fetchServers()).rejects.toThrow();
  });

  it.each([
    [{}],
    [{ servers: 'nope' }],
    [{ servers: [{ name: 'x', label: 'x', color_valid: '#0f0' }] }],
    [
      {
        servers: [
          { name: 1, label: 'x', color_valid: '#0f0', color_invalid: '#f00' }
        ]
      }
    ]
  ])('rejects a malformed servers payload %#', async payload => {
    stubFetch(jsonResponse(payload));
    await expect(fetchServers()).rejects.toThrow();
  });
});

describe('fetchStatus', () => {
  it('requests the status route for the encoded server name', async () => {
    const mock = stubFetch(jsonResponse(STATUS_PAYLOAD));
    const status = await fetchStatus('alpha/beta');
    expect(status).toEqual(STATUS_PAYLOAD);
    const [url, init] = mock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/services/tessera/status?server=alpha%2Fbeta');
    expect(init.credentials).toBe('omit');
  });

  it('accepts the red all-null status shape', async () => {
    const red = {
      has_token: false,
      expires_at: null,
      created_at: null,
      updated_at: null,
      scope: null,
      refresh_kind: 'unknown',
      refresh_expires_at: null
    };
    stubFetch(jsonResponse(red));
    await expect(fetchStatus('stable-discovery')).resolves.toEqual(red);
  });

  it.each([
    [{ ...STATUS_PAYLOAD, refresh_kind: 'weird' }],
    [{ ...STATUS_PAYLOAD, has_token: 'yes' }],
    [{ ...STATUS_PAYLOAD, expires_at: 'soon' }],
    [{ ...STATUS_PAYLOAD, scope: 42 }],
    ['not-an-object']
  ])('rejects a malformed status payload %#', async payload => {
    stubFetch(jsonResponse(payload));
    await expect(fetchStatus('stable-discovery')).rejects.toThrow();
  });

  it('rejects a non-ok status response', async () => {
    stubFetch(jsonResponse({ error: 'unknown_server' }, { status: 404 }));
    await expect(fetchStatus('nowhere')).rejects.toThrow();
  });
});

describe('postVerify', () => {
  it('POSTs the verify action with the API token and returns the verdict', async () => {
    const mock = stubFetch(jsonResponse({ valid: true }));
    const result = await postVerify('alpha');
    expect(result).toEqual({ valid: true });
    const [url, init] = mock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/services/tessera/verify?server=alpha');
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('omit');
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBe(`token ${TEST_TOKEN}`);
  });

  it('returns the negative verdict untouched', async () => {
    stubFetch(jsonResponse({ valid: false }));
    await expect(postVerify('alpha')).resolves.toEqual({ valid: false });
  });

  it('exposes the 502 of an unreachable provider', async () => {
    stubFetch(jsonResponse({ error: 'refresh_unavailable' }, { status: 502 }));
    await expect(postVerify('alpha')).rejects.toMatchObject({ status: 502 });
  });

  it.each([[{ valid: 'yes' }], [{}], ['nope']])(
    'rejects a malformed verify payload %#',
    async payload => {
      stubFetch(jsonResponse(payload));
      await expect(postVerify('alpha')).rejects.toThrow();
    }
  );
});

describe('postRevoke', () => {
  it('POSTs the revoke action and returns the honest outcome', async () => {
    const mock = stubFetch(
      jsonResponse({ revoked: true, provider_notified: false })
    );
    const result = await postRevoke('alpha');
    expect(result).toEqual({ revoked: true, provider_notified: false });
    const [url, init] = mock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/services/tessera/revoke?server=alpha');
    expect(init.method).toBe('POST');
  });

  it.each([
    [{ revoked: true }],
    [{ provider_notified: true }],
    [{ revoked: 1, provider_notified: true }]
  ])('rejects a malformed revoke payload %#', async payload => {
    stubFetch(jsonResponse(payload));
    await expect(postRevoke('alpha')).rejects.toThrow();
  });

  it('exposes the 409 of a double revoke', async () => {
    stubFetch(jsonResponse({ error: 'no_stored_token' }, { status: 409 }));
    await expect(postRevoke('alpha')).rejects.toMatchObject({ status: 409 });
  });
});

describe('ServiceError', () => {
  it('carries a null status for malformed payloads', async () => {
    stubFetch(jsonResponse({}));
    try {
      await fetchServers();
      expect.unreachable('fetchServers must reject on a malformed payload');
    } catch (error) {
      expect(error).toBeInstanceOf(ServiceError);
      expect((error as ServiceError).status).toBeNull();
    }
  });
});
