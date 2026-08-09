/**
 * Tests for the sidebar panel (`TesseraPanel`).
 *
 * Observable behavior with a routed fetch stub, fake timers, and mocked
 * JupyterLab notifications/dialogs: list rendering, per-server coloring
 * (including the opportunistic expiry warning), the poll and visibility
 * refreshes, the verify and revoke actions (confirmation, busy lock,
 * honest toasts, immediate re-poll), the sober degraded states (service
 * unreachable, missing scopes, page without a token), the displayed
 * version, and the strict cleanup on unmount.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { PageConfig } from '@jupyterlab/coreutils';
import { act, fireEvent, render, screen } from '@testing-library/react';
import * as React from 'react';

import { PANEL_VERSION } from '../version';
import { TesseraPanel } from './TesseraPanel';

vi.mock('@jupyterlab/apputils', async importOriginal => {
  const actual = await importOriginal<typeof import('@jupyterlab/apputils')>();
  return {
    ...actual,
    showDialog: vi.fn(),
    Notification: {
      success: vi.fn(),
      warning: vi.fn(),
      error: vi.fn(),
      info: vi.fn()
    }
  };
});

import { Notification, showDialog } from '@jupyterlab/apputils';

const NOW_MS = 1_800_000_000_000; // fixed wall clock for every test
const NOW = NOW_MS / 1000;

const SERVERS_PAYLOAD = {
  servers: [
    {
      name: 'alpha',
      label: 'Alpha IdP',
      color_valid: '#10a010',
      color_invalid: '#a01010'
    },
    {
      name: 'beta',
      label: 'Beta IdP',
      color_valid: '#20b020',
      color_invalid: '#b02020'
    }
  ]
};

const GREEN_STATUS = {
  has_token: true,
  expires_at: NOW + 42,
  created_at: NOW - 3600,
  updated_at: NOW - 60,
  scope: 'openid',
  refresh_kind: 'session',
  refresh_expires_at: null
};

const RED_STATUS = {
  has_token: false,
  expires_at: null,
  created_at: null,
  updated_at: null,
  scope: null,
  refresh_kind: 'unknown',
  refresh_expires_at: null
};

// Window 300 s, threshold 75 s, 50 s left: inside the warning band.
const EXPIRING_STATUS = {
  ...GREEN_STATUS,
  updated_at: NOW - 250,
  refresh_expires_at: NOW + 50
};

function jsonResponse(payload: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init
  });
}

interface StubConfig {
  statuses: Record<string, unknown>;
  servers?: Response;
  verify?: () => Promise<Response> | Response;
  revoke?: () => Promise<Response> | Response;
}

interface FetchStub {
  mock: ReturnType<typeof vi.fn>;
  config: StubConfig;
  statusCalls: () => number;
  verifyCalls: () => number;
  revokeCalls: () => number;
  statusSignals: AbortSignal[];
}

/** Stub fetch routing the four service routes from a mutable config. */
function stubRoutedFetch(config: StubConfig): FetchStub {
  const statusSignals: AbortSignal[] = [];
  /** Serve the default /status route: record the signal, answer from config. */
  const statusRoute = (url: string, init?: RequestInit): Response => {
    if (init?.signal) {
      statusSignals.push(init.signal);
    }
    const name = decodeURIComponent(url.split('server=')[1] ?? '');
    const payload = config.statuses[name];
    if (payload === undefined) {
      return new Response('boom', { status: 500 });
    }
    return jsonResponse(payload);
  };
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith('/servers')) {
      return config.servers ?? jsonResponse(SERVERS_PAYLOAD);
    }
    if (url.includes('/verify')) {
      return config.verify ? config.verify() : jsonResponse({ valid: true });
    }
    if (url.includes('/revoke')) {
      return config.revoke
        ? config.revoke()
        : jsonResponse({ revoked: true, provider_notified: true });
    }
    return statusRoute(url, init);
  });
  vi.stubGlobal('fetch', mock);
  const calls = (needle: string) => () =>
    mock.mock.calls.filter(call => String(call[0]).includes(needle)).length;
  return {
    mock,
    config,
    statusCalls: calls('/status'),
    verifyCalls: calls('/verify'),
    revokeCalls: calls('/revoke'),
    statusSignals
  };
}

/** Flush the pending microtask chains (fetch -> json -> setState). */
async function flush(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 10; i++) {
      await Promise.resolve();
    }
  });
}

function item(name: string): HTMLElement {
  const node = document.querySelector(
    `.tessera-ServerItem[data-server="${name}"]`
  );
  expect(node).not.toBeNull();
  return node as HTMLElement;
}

function actionButton(name: string, label: RegExp): HTMLButtonElement {
  const button = item(name).querySelector(
    `button[aria-label]`
  ) as HTMLButtonElement | null;
  const buttons = Array.from(item(name).querySelectorAll('button'));
  const match = buttons.find(candidate =>
    label.test(
      candidate.getAttribute('aria-label') ?? candidate.textContent ?? ''
    )
  );
  expect(match, `button ${label} on ${name}`).toBeDefined();
  void button;
  return match as HTMLButtonElement;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW_MS);
  PageConfig.setOption('token', 'test-hub-api-token');
  vi.mocked(showDialog).mockReset();
  vi.mocked(Notification.success).mockReset();
  vi.mocked(Notification.warning).mockReset();
  vi.mocked(Notification.error).mockReset();
  vi.mocked(Notification.info).mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  PageConfig.setOption('token', '');
});

describe('TesseraPanel rendering', () => {
  it('renders the header, the version, and the servers in order', async () => {
    stubRoutedFetch({ statuses: { alpha: GREEN_STATUS, beta: RED_STATUS } });
    render(<TesseraPanel />);
    await flush();
    expect(screen.getByText('TESSERA')).toBeDefined();
    expect(screen.getByText(`v${PANEL_VERSION}`)).toBeDefined();
    const labels = screen
      .getAllByRole('listitem')
      .map(node => node.textContent);
    expect(labels[0]).toContain('Alpha IdP');
    expect(labels[1]).toContain('Beta IdP');
  });

  it('paints each server from its polled status', async () => {
    stubRoutedFetch({ statuses: { alpha: GREEN_STATUS, beta: RED_STATUS } });
    render(<TesseraPanel />);
    await flush();
    expect(item('alpha').dataset.status).toBe('valid');
    expect(item('beta').dataset.status).toBe('invalid');
  });

  it('warns in orange when the dated refresh expiry gets close', async () => {
    stubRoutedFetch({
      statuses: { alpha: EXPIRING_STATUS, beta: GREEN_STATUS }
    });
    render(<TesseraPanel />);
    await flush();
    expect(item('alpha').dataset.status).toBe('expiring');
    expect(item('beta').dataset.status).toBe('valid');
  });

  it('keeps a failing status sober: indeterminate, never a lying color', async () => {
    stubRoutedFetch({ statuses: { alpha: GREEN_STATUS } }); // beta answers 500
    render(<TesseraPanel />);
    await flush();
    expect(item('beta').dataset.status).toBe('unknown');
  });

  it('shows a sober message when the service is unreachable', async () => {
    stubRoutedFetch({
      statuses: {},
      servers: new Response('gone', { status: 502 })
    });
    render(<TesseraPanel />);
    await flush();
    expect(screen.getByText(/cannot reach the tessera service/i)).toBeDefined();
    expect(screen.queryAllByRole('listitem')).toHaveLength(0);
  });

  it('points at the missing-scope configuration on a 403', async () => {
    stubRoutedFetch({
      statuses: {},
      servers: new Response('forbidden', { status: 403 })
    });
    render(<TesseraPanel />);
    await flush();
    expect(screen.getByText(/oauth_client_allowed_scopes/)).toBeDefined();
    expect(screen.getByText(/access:services!service=tessera/)).toBeDefined();
  });

  it('degrades soberly on a page without an API token', async () => {
    PageConfig.setOption('token', '');
    // Without a credential the chain ends on a login page, not JSON.
    stubRoutedFetch({
      statuses: {},
      servers: new Response('<html>login</html>', {
        status: 200,
        headers: { 'Content-Type': 'text/html' }
      })
    });
    render(<TesseraPanel />);
    await flush();
    expect(screen.getByText(/cannot reach the tessera service/i)).toBeDefined();
  });
});

describe('TesseraPanel refresh cycle', () => {
  it('polls every 30 seconds', async () => {
    const stub = stubRoutedFetch({
      statuses: { alpha: GREEN_STATUS, beta: RED_STATUS }
    });
    render(<TesseraPanel />);
    await flush();
    expect(stub.statusCalls()).toBe(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    await flush();
    expect(stub.statusCalls()).toBe(4);
  });

  it('refreshes immediately when the tab becomes visible again', async () => {
    const stub = stubRoutedFetch({
      statuses: { alpha: GREEN_STATUS, beta: RED_STATUS }
    });
    render(<TesseraPanel />);
    await flush();
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await flush();
    expect(stub.statusCalls()).toBe(4);
  });

  it('does not refresh when the tab goes hidden', async () => {
    const stub = stubRoutedFetch({
      statuses: { alpha: GREEN_STATUS, beta: RED_STATUS }
    });
    render(<TesseraPanel />);
    await flush();
    expect(stub.statusCalls()).toBe(2);
    const visibility = vi
      .spyOn(document, 'visibilityState', 'get')
      .mockReturnValue('hidden');
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await flush();
    expect(stub.statusCalls()).toBe(2); // hidden: no refresh fired
    visibility.mockRestore();
  });

  it('an aborted refresh never repaints the panel', async () => {
    const stub = stubRoutedFetch({
      statuses: { alpha: GREEN_STATUS, beta: GREEN_STATUS }
    });
    interface Gate {
      resolve: (response: Response) => void;
      reject: (error: unknown) => void;
    }
    const gate = { pending: [] as Gate[] };
    const realImpl = stub.mock.getMockImplementation()!;
    stub.mock.mockImplementation(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/status') && stub.statusSignals.length < 2) {
          if (init?.signal) {
            stub.statusSignals.push(init.signal);
          }
          return new Promise<Response>((resolve, reject) => {
            gate.pending.push({ resolve, reject });
          });
        }
        return realImpl(input, init);
      }
    );
    render(<TesseraPanel />);
    await flush();
    expect(gate.pending).toHaveLength(2);
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await flush();
    expect(item('alpha').dataset.status).toBe('valid');
    expect(stub.statusSignals[0].aborted).toBe(true);
    gate.pending[0].resolve(jsonResponse(RED_STATUS));
    gate.pending[1].reject(new DOMException('aborted', 'AbortError'));
    await flush();
    expect(item('alpha').dataset.status).toBe('valid');
    expect(item('beta').dataset.status).toBe('valid');
  });
});

describe('TesseraPanel sign-in', () => {
  it('opens the login flow in a new tab for a red server', async () => {
    stubRoutedFetch({ statuses: { alpha: GREEN_STATUS, beta: RED_STATUS } });
    const open = vi.fn();
    vi.stubGlobal('open', open);
    render(<TesseraPanel />);
    await flush();
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: /sign in/i }));
    });
    expect(open).toHaveBeenCalledWith(
      '/services/tessera/login?server=beta',
      '_blank',
      'noopener'
    );
  });
});

describe('TesseraPanel verify action', () => {
  it('verifies, toasts the good news, and re-polls', async () => {
    const stub = stubRoutedFetch({
      statuses: { alpha: GREEN_STATUS, beta: GREEN_STATUS }
    });
    render(<TesseraPanel />);
    await flush();
    const before = stub.statusCalls();
    act(() => {
      fireEvent.click(actionButton('alpha', /verify/i));
    });
    await flush();
    expect(stub.verifyCalls()).toBe(1);
    expect(Notification.success).toHaveBeenCalledWith(
      'Token valid',
      expect.anything()
    );
    expect(stub.statusCalls()).toBe(before + 2); // immediate re-poll
  });

  it('reports an invalid token and repaints red after the re-poll', async () => {
    const stub = stubRoutedFetch({
      statuses: { alpha: GREEN_STATUS, beta: GREEN_STATUS },
      verify: () => jsonResponse({ valid: false })
    });
    render(<TesseraPanel />);
    await flush();
    stub.config.statuses.alpha = RED_STATUS; // the refresh path purged it
    act(() => {
      fireEvent.click(actionButton('alpha', /verify/i));
    });
    await flush();
    expect(Notification.warning).toHaveBeenCalledWith(
      'Sign in required',
      expect.anything()
    );
    expect(item('alpha').dataset.status).toBe('invalid');
  });

  it('stays sober when the provider cannot be reached (502)', async () => {
    stubRoutedFetch({
      statuses: { alpha: GREEN_STATUS, beta: GREEN_STATUS },
      verify: () =>
        jsonResponse({ error: 'refresh_unavailable' }, { status: 502 })
    });
    render(<TesseraPanel />);
    await flush();
    act(() => {
      fireEvent.click(actionButton('alpha', /verify/i));
    });
    await flush();
    expect(Notification.error).toHaveBeenCalledWith(
      'The provider could not be reached. Try again shortly.',
      expect.anything()
    );
    expect(item('alpha').dataset.status).toBe('valid'); // unchanged
  });

  it('stays sober on any other verify failure', async () => {
    stubRoutedFetch({
      statuses: { alpha: GREEN_STATUS, beta: GREEN_STATUS },
      verify: () => new Response('boom', { status: 500 })
    });
    render(<TesseraPanel />);
    await flush();
    act(() => {
      fireEvent.click(actionButton('alpha', /verify/i));
    });
    await flush();
    expect(Notification.error).toHaveBeenCalledWith(
      'Verification failed. Try again shortly.',
      expect.anything()
    );
  });

  it('sends a single POST on a double click', async () => {
    let release: ((response: Response) => void) | null = null;
    const stub = stubRoutedFetch({
      statuses: { alpha: GREEN_STATUS, beta: GREEN_STATUS },
      verify: () =>
        new Promise<Response>(resolve => {
          release = resolve;
        })
    });
    render(<TesseraPanel />);
    await flush();
    act(() => {
      fireEvent.click(actionButton('alpha', /verify/i));
    });
    await flush();
    act(() => {
      fireEvent.click(actionButton('alpha', /verify/i)); // busy: locked
    });
    await flush();
    expect(stub.verifyCalls()).toBe(1);
    act(() => {
      release?.(jsonResponse({ valid: true }));
    });
    await flush();
    expect(Notification.success).toHaveBeenCalledTimes(1);
  });
});

describe('TesseraPanel revoke action', () => {
  it('asks for confirmation and does nothing when cancelled', async () => {
    const stub = stubRoutedFetch({
      statuses: { alpha: GREEN_STATUS, beta: GREEN_STATUS }
    });
    vi.mocked(showDialog).mockResolvedValue({
      button: { accept: false }
    } as never);
    render(<TesseraPanel />);
    await flush();
    act(() => {
      fireEvent.click(actionButton('alpha', /revoke/i));
    });
    await flush();
    expect(showDialog).toHaveBeenCalledTimes(1);
    expect(stub.revokeCalls()).toBe(0);
  });

  it('revokes, toasts the provider-confirmed outcome, and repaints red', async () => {
    const stub = stubRoutedFetch({
      statuses: { alpha: GREEN_STATUS, beta: GREEN_STATUS }
    });
    vi.mocked(showDialog).mockResolvedValue({
      button: { accept: true }
    } as never);
    render(<TesseraPanel />);
    await flush();
    stub.config.statuses.alpha = RED_STATUS; // purged after the revoke
    act(() => {
      fireEvent.click(actionButton('alpha', /revoke/i));
    });
    await flush();
    expect(stub.revokeCalls()).toBe(1);
    expect(Notification.success).toHaveBeenCalledWith(
      'Token revoked',
      expect.anything()
    );
    expect(item('alpha').dataset.status).toBe('invalid');
  });

  it('stays honest when the provider could not be notified', async () => {
    stubRoutedFetch({
      statuses: { alpha: GREEN_STATUS, beta: GREEN_STATUS },
      revoke: () => jsonResponse({ revoked: true, provider_notified: false })
    });
    vi.mocked(showDialog).mockResolvedValue({
      button: { accept: true }
    } as never);
    render(<TesseraPanel />);
    await flush();
    act(() => {
      fireEvent.click(actionButton('alpha', /revoke/i));
    });
    await flush();
    expect(Notification.warning).toHaveBeenCalledWith(
      'Removed locally; the provider could not be notified',
      expect.anything()
    );
  });

  it('stays sober when the revoke request itself fails', async () => {
    stubRoutedFetch({
      statuses: { alpha: GREEN_STATUS, beta: GREEN_STATUS },
      revoke: () => new Response('boom', { status: 500 })
    });
    vi.mocked(showDialog).mockResolvedValue({
      button: { accept: true }
    } as never);
    render(<TesseraPanel />);
    await flush();
    act(() => {
      fireEvent.click(actionButton('alpha', /revoke/i));
    });
    await flush();
    expect(Notification.error).toHaveBeenCalledWith(
      'Revocation failed. Try again shortly.',
      expect.anything()
    );
  });
});

describe('TesseraPanel copy action', () => {
  it('copies the notebook snippet and toasts', async () => {
    stubRoutedFetch({ statuses: { alpha: GREEN_STATUS, beta: GREEN_STATUS } });
    render(<TesseraPanel />);
    await flush();
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    act(() => {
      fireEvent.click(actionButton('alpha', /copy/i));
    });
    await flush();
    expect(writeText).toHaveBeenCalledWith('TESSERA_TOKEN["alpha"]');
    expect(Notification.info).toHaveBeenCalledWith(
      'Copied the notebook snippet',
      expect.anything()
    );
  });

  it('stays sober when the clipboard write is refused', async () => {
    stubRoutedFetch({ statuses: { alpha: GREEN_STATUS, beta: GREEN_STATUS } });
    render(<TesseraPanel />);
    await flush();
    const writeText = vi.fn().mockRejectedValue(new Error('denied'));
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    act(() => {
      fireEvent.click(actionButton('alpha', /copy/i));
    });
    await flush();
    expect(Notification.error).toHaveBeenCalledWith(
      'Could not copy the snippet',
      expect.anything()
    );
  });
});

describe('TesseraPanel cleanup', () => {
  it('stops polling once unmounted', async () => {
    const stub = stubRoutedFetch({
      statuses: { alpha: GREEN_STATUS, beta: RED_STATUS }
    });
    const { unmount } = render(<TesseraPanel />);
    await flush();
    expect(stub.statusCalls()).toBe(2);
    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(90_000);
    });
    expect(stub.statusCalls()).toBe(2);
  });

  it('stops listening to visibility changes once unmounted', async () => {
    const stub = stubRoutedFetch({
      statuses: { alpha: GREEN_STATUS, beta: RED_STATUS }
    });
    const { unmount } = render(<TesseraPanel />);
    await flush();
    unmount();
    document.dispatchEvent(new Event('visibilitychange'));
    await flush();
    expect(stub.statusCalls()).toBe(2);
  });

  it('aborts the in-flight refresh on unmount', async () => {
    const stub = stubRoutedFetch({ statuses: {} });
    stub.mock.mockImplementation(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith('/servers')) {
          return jsonResponse(SERVERS_PAYLOAD);
        }
        if (init?.signal) {
          stub.statusSignals.push(init.signal);
        }
        return new Promise<Response>(() => undefined);
      }
    );
    const { unmount } = render(<TesseraPanel />);
    await flush();
    expect(stub.statusSignals.length).toBeGreaterThan(0);
    unmount();
    expect(stub.statusSignals.every(signal => signal.aborted)).toBe(true);
  });

  it('an aborted topology load after unmount stays silent', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    let rejectServers: ((reason: unknown) => void) | null = null;
    const stub = stubRoutedFetch({ statuses: {} });
    stub.mock.mockImplementation(
      async () =>
        new Promise<Response>((_resolve, reject) => {
          rejectServers = reject;
        })
    );
    const { unmount } = render(<TesseraPanel />);
    expect(rejectServers).not.toBeNull();
    unmount(); // the cleanup aborts the in-flight topology load
    act(() => {
      rejectServers?.(
        new DOMException('The operation was aborted.', 'AbortError')
      );
    });
    await flush();
    // The unmounted panel never repaints and nothing errors in flight.
    expect(document.querySelectorAll('.tessera-ServerItem')).toHaveLength(0);
    expect(errorSpy).not.toHaveBeenCalled();
    errorSpy.mockRestore();
  });
});
