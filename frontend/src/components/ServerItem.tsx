import * as React from 'react';

import type { ServerDescriptor, TokenStatusPayload } from '../api';
import type { DisplayStatus } from '../status';
import { ServerDetails } from './ServerDetails';

/**
 * Props of the {@link ServerItem} component.
 */
export interface ServerItemProps {
  /** The declared server this entry represents. */
  descriptor: ServerDescriptor;
  /** Current visual state of the entry. */
  status: DisplayStatus;
  /** The last polled payload, when one was read. */
  details: TokenStatusPayload | null;
  /** Whether a network action is in flight for this server. */
  busy: boolean;
  /** The current Unix time in seconds, for the detail countdown. */
  now: number;
  /** Called when the user asks to sign in (invalid state only). */
  onLogin: () => void;
  /** Called when the user asks to verify the stored token. */
  onVerify: () => void;
  /** Called when the user asks to revoke the stored token. */
  onRevoke: () => void;
  /** Called when the user asks to copy the notebook snippet. */
  onCopy: () => void;
}

/** Resolve the dot color for a display status, when the state defines one. */
function dotColorOf(
  status: ServerItemProps['status'],
  descriptor: ServerDescriptor
): string | undefined {
  switch (status) {
    case 'valid':
      return descriptor.color_valid;
    case 'invalid':
      return descriptor.color_invalid;
    case 'expiring':
      return 'var(--jp-warn-color1)';
    default:
      return undefined;
  }
}

/**
 * One server entry of the tessera panel: dot, label, actions, details.
 *
 * @param props - Component props; see {@link ServerItemProps}.
 * @returns The list item for the server.
 *
 * @remarks
 * The dot takes the server's configured colors for the valid and
 * invalid states, the theme warn color when the provider-dated refresh
 * expiry enters its warning band, and stays theme-neutral while
 * indeterminate. Sign-in is offered only when invalid; details, verify,
 * and revoke only when a token is presumed present (valid or expiring),
 * with the network actions locked while one is in flight. The token
 * itself never reaches this component.
 */
export function ServerItem(props: ServerItemProps): React.ReactElement {
  const { descriptor, status, details, busy } = props;
  const [expanded, setExpanded] = React.useState(false);
  const color = dotColorOf(status, descriptor);
  const dotStyle =
    color === undefined
      ? undefined
      : ({ '--tessera-dot': color } as React.CSSProperties);
  const hasActions =
    (status === 'valid' || status === 'expiring') && details !== null;
  return (
    <li
      className="tessera-ServerItem"
      data-server={descriptor.name}
      data-status={status}
    >
      <div className="tessera-ServerItem-row">
        <span
          className="tessera-ServerItem-dot"
          style={dotStyle}
          aria-hidden="true"
        />
        <span className="tessera-ServerItem-label">{descriptor.label}</span>
        {status === 'invalid' && (
          <button
            type="button"
            className="tessera-ServerItem-signin"
            onClick={props.onLogin}
          >
            Sign in
          </button>
        )}
        {hasActions && (
          <span className="tessera-ServerItem-actions">
            <button
              type="button"
              className="tessera-ServerItem-action"
              aria-label="Details"
              title="Details"
              onClick={() => setExpanded(previous => !previous)}
            >
              i
            </button>
            <button
              type="button"
              className="tessera-ServerItem-action"
              aria-label="Copy"
              title="Copy the notebook snippet"
              onClick={props.onCopy}
            >
              &#10697;
            </button>
            <button
              type="button"
              className="tessera-ServerItem-action"
              aria-label="Verify"
              title="Verify the token against the provider"
              disabled={busy}
              onClick={props.onVerify}
            >
              &#8635;
            </button>
            <button
              type="button"
              className="tessera-ServerItem-action tessera-ServerItem-action-danger"
              aria-label="Revoke"
              title="Revoke the token"
              disabled={busy}
              onClick={props.onRevoke}
            >
              &#10005;
            </button>
          </span>
        )}
      </div>
      {expanded && details !== null && (
        <ServerDetails details={details} now={props.now} />
      )}
    </li>
  );
}
