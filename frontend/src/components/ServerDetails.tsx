import * as React from 'react';

import type { RefreshKind, TokenStatusPayload } from '../api';
import { formatRemaining, formatTimestamp } from '../status';

const KIND_LABELS: Record<RefreshKind, string> = {
  offline: 'Offline',
  session: 'Session-bound',
  unknown: 'Unknown'
};

/**
 * Props of the {@link ServerDetails} component.
 */
export interface ServerDetailsProps {
  /** The polled status payload to detail. */
  details: TokenStatusPayload;
  /** The current Unix time in seconds, for the countdown. */
  now: number;
}

/**
 * The unfolded detail block of a server entry.
 *
 * @param props - Component props; see {@link ServerDetailsProps}.
 * @returns The definition list of the known facts.
 *
 * @remarks
 * Facts only, soberly: null fields are omitted rather than rendered as
 * placeholders, timestamps are stable UTC stamps, and the access-token
 * countdown is informative only (it never drives a color, the token
 * auto-refreshes on use). No token value ever reaches this component.
 */
export function ServerDetails(props: ServerDetailsProps): React.ReactElement {
  const { details, now } = props;
  const rows: Array<[string, string]> = [];
  if (details.created_at !== null) {
    rows.push(['Signed in', formatTimestamp(details.created_at)]);
  }
  if (details.updated_at !== null) {
    rows.push(['Last refresh', formatTimestamp(details.updated_at)]);
  }
  if (details.expires_at !== null) {
    rows.push(['Access token', formatRemaining(details.expires_at - now)]);
  }
  if (details.scope !== null) {
    rows.push(['Scopes', details.scope]);
  }
  rows.push(['Refresh token', KIND_LABELS[details.refresh_kind]]);
  if (details.refresh_expires_at !== null) {
    rows.push(['Refresh expires', formatTimestamp(details.refresh_expires_at)]);
  }
  return (
    <dl className="tessera-ServerDetails">
      {rows.map(([label, value]) => (
        <React.Fragment key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </React.Fragment>
      ))}
    </dl>
  );
}
