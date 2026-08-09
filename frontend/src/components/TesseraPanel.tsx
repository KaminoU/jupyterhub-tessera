import { Dialog, Notification, showDialog } from '@jupyterlab/apputils';
import * as React from 'react';

import {
  ServiceError,
  fetchServers,
  fetchStatus,
  loginUrl,
  postRevoke,
  postVerify
} from '../api';
import type { ServerDescriptor, TokenStatusPayload } from '../api';
import { tesseraIcon } from '../icon';
import { displayStatusOf } from '../status';
import { PANEL_VERSION } from '../version';
import { ServerItem } from './ServerItem';

/**
 * Poll period of the per-server status, in milliseconds.
 */
export const POLL_INTERVAL_MS = 30_000;

/** Entry state of the panel: the outcome of the topology load. */
type EntryState = 'loading' | 'ready' | 'unreachable' | 'forbidden';

/**
 * Props of the {@link TesseraPanel} component.
 */
export interface TesseraPanelProps {
  /** Status poll period in milliseconds. Defaults to {@link POLL_INTERVAL_MS}. */
  pollIntervalMs?: number;
}

/**
 * The tessera sidebar panel: one live status entry per declared server.
 *
 * @param props - Component props; see {@link TesseraPanelProps}.
 * @returns The panel content.
 *
 * @remarks
 * The topology (`/servers`) is fetched once at mount; statuses are
 * polled on a single interval, refreshed immediately when the tab
 * becomes visible again and after every action. The expiry warning is
 * recomputed at each poll repaint from the provider-dated facts, with
 * no extra timer and no network. Actions are locked per server while
 * one is in flight; their toasts report states only, never a value.
 * A revocation asks for a native confirmation first (it cannot be
 * undone). Degraded entries are sober and actionable: a 403 points at
 * the missing RBAC grant, anything else at the service. Everything set
 * up here is torn down on unmount (interval, listener, in-flight
 * requests).
 */
export function TesseraPanel(props: TesseraPanelProps): React.ReactElement {
  const pollIntervalMs = props.pollIntervalMs ?? POLL_INTERVAL_MS;
  const [servers, setServers] = React.useState<ServerDescriptor[] | null>(null);
  const [entry, setEntry] = React.useState<EntryState>('loading');
  const [statuses, setStatuses] = React.useState<
    Record<string, TokenStatusPayload | 'error'>
  >({});
  const [busy, setBusy] = React.useState<Record<string, boolean>>({});
  const controllerRef = React.useRef<AbortController | null>(null);

  const refresh = React.useCallback((): void => {
    /* v8 ignore next 3 -- unreachable: both callers guard servers !== null (the poll effect installs only when servers is non-null, and runAction only fires from ServerItem buttons, rendered only when servers is non-null) */
    if (servers === null) {
      return;
    }
    controllerRef.current?.abort();
    const current = new AbortController();
    controllerRef.current = current;
    for (const server of servers) {
      fetchStatus(server.name, current.signal)
        .then(status => {
          if (current.signal.aborted) {
            return; // settled just before the abort: stale, ignore
          }
          setStatuses(previous => ({ ...previous, [server.name]: status }));
        })
        .catch(() => {
          if (current.signal.aborted) {
            return; // a stale wave never repaints the panel
          }
          setStatuses(previous => ({ ...previous, [server.name]: 'error' }));
        });
    }
  }, [servers]);

  React.useEffect(() => {
    const controller = new AbortController();
    fetchServers(controller.signal)
      .then(list => {
        setServers(list);
        setEntry('ready');
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        const denied = error instanceof ServiceError && error.status === 403;
        setEntry(denied ? 'forbidden' : 'unreachable');
      });
    return () => {
      controller.abort();
    };
  }, []);

  React.useEffect(() => {
    if (servers === null) {
      return;
    }
    refresh();
    const timer = window.setInterval(refresh, pollIntervalMs);
    const onVisibilityChange = (): void => {
      if (document.visibilityState === 'visible') {
        refresh();
      }
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibilityChange);
      controllerRef.current?.abort();
    };
  }, [servers, pollIntervalMs, refresh]);

  const runAction = async (
    name: string,
    action: () => Promise<void>
  ): Promise<void> => {
    /* v8 ignore next 3 -- unreachable via the DOM: the action buttons are disabled while busy, and handlers fired before the re-render close over a stale busy; kept as a belt for a future programmatic caller (revoke is destructive) */
    if (busy[name]) {
      return;
    }
    setBusy(previous => ({ ...previous, [name]: true }));
    try {
      await action();
    } finally {
      setBusy(previous => ({ ...previous, [name]: false }));
      refresh();
    }
  };

  const handleVerify = (server: ServerDescriptor): void => {
    void runAction(server.name, async () => {
      try {
        const verdict = await postVerify(server.name);
        if (verdict.valid) {
          Notification.success('Token valid', { autoClose: 3000 });
        } else {
          Notification.warning('Sign in required', { autoClose: 5000 });
        }
      } catch (error) {
        if (error instanceof ServiceError && error.status === 502) {
          Notification.error(
            'The provider could not be reached. Try again shortly.',
            { autoClose: 5000 }
          );
        } else {
          Notification.error('Verification failed. Try again shortly.', {
            autoClose: 5000
          });
        }
      }
    });
  };

  const handleRevoke = async (server: ServerDescriptor): Promise<void> => {
    const result = await showDialog({
      title: `Revoke the ${server.label} token?`,
      body:
        'The stored token will be revoked at the provider when possible ' +
        'and removed locally. This cannot be undone.',
      buttons: [Dialog.cancelButton(), Dialog.warnButton({ label: 'Revoke' })]
    });
    if (!result.button.accept) {
      return; // cancelled: no call at all
    }
    await runAction(server.name, async () => {
      try {
        const outcome = await postRevoke(server.name);
        if (outcome.provider_notified) {
          Notification.success('Token revoked', { autoClose: 3000 });
        } else {
          Notification.warning(
            'Removed locally; the provider could not be notified',
            { autoClose: 5000 }
          );
        }
      } catch {
        Notification.error('Revocation failed. Try again shortly.', {
          autoClose: 5000
        });
      }
    });
  };

  const handleCopy = async (server: ServerDescriptor): Promise<void> => {
    try {
      await navigator.clipboard.writeText(`TESSERA_TOKEN["${server.name}"]`);
      Notification.info('Copied the notebook snippet', { autoClose: 3000 });
    } catch {
      Notification.error('Could not copy the snippet', { autoClose: 3000 });
    }
  };

  const now = Date.now() / 1000;

  return (
    <div className="tessera-Panel">
      <div className="tessera-Panel-header">
        <tesseraIcon.react
          tag="span"
          className="tessera-Panel-headerIcon"
          height="20px"
          width="20px"
        />
        <span className="tessera-Panel-title">TESSERA</span>
        <span className="tessera-Panel-version">v{PANEL_VERSION}</span>
      </div>
      {entry === 'unreachable' && (
        <p className="tessera-Panel-message">
          Cannot reach the tessera service. Reload the page or contact your
          JupyterHub administrator.
        </p>
      )}
      {entry === 'forbidden' && (
        <p className="tessera-Panel-message">
          The panel is not authorized to reach the tessera service: grant the
          page token the access:services!service=tessera scope with
          c.Spawner.oauth_client_allowed_scopes (see the tessera documentation).
        </p>
      )}
      {servers !== null && (
        <ul className="tessera-Panel-list">
          {servers.map(server => {
            const status = statuses[server.name];
            const display = displayStatusOf(status, now);
            return (
              <ServerItem
                key={server.name}
                descriptor={server}
                status={display}
                details={
                  status === undefined || status === 'error' ? null : status
                }
                busy={busy[server.name] === true}
                now={now}
                onLogin={() => {
                  window.open(loginUrl(server.name), '_blank', 'noopener');
                }}
                onVerify={() => {
                  handleVerify(server);
                }}
                onRevoke={() => {
                  void handleRevoke(server);
                }}
                onCopy={() => {
                  void handleCopy(server);
                }}
              />
            );
          })}
        </ul>
      )}
    </div>
  );
}
