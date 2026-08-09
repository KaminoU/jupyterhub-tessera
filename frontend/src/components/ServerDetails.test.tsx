/**
 * Tests for the per-server detail block (`ServerDetails`).
 *
 * Observable behavior: known fields render as labeled rows with
 * deterministic UTC formatting, null fields are omitted entirely, and
 * the access-token countdown is informative (it never drives a color).
 */

import { describe, expect, it } from 'vitest';

import { render, screen } from '@testing-library/react';
import * as React from 'react';

import type { TokenStatusPayload } from '../api';
import { ServerDetails } from './ServerDetails';

const NOW = 1_800_000_000;

const FULL: TokenStatusPayload = {
  has_token: true,
  expires_at: NOW + 42,
  created_at: 1_800_000_000 - 3600,
  updated_at: 1_800_000_000 - 600,
  scope: 'openid profile offline_access',
  refresh_kind: 'offline',
  refresh_expires_at: NOW + 7500
};

describe('ServerDetails', () => {
  it('renders every known field with readable values', () => {
    render(<ServerDetails details={FULL} now={NOW} />);
    expect(screen.getByText('Signed in')).toBeDefined();
    expect(screen.getByText('2027-01-15 07:00 UTC')).toBeDefined();
    expect(screen.getByText('Last refresh')).toBeDefined();
    expect(screen.getByText('2027-01-15 07:50 UTC')).toBeDefined();
    expect(screen.getByText('Access token')).toBeDefined();
    expect(screen.getByText('in 42 s')).toBeDefined();
    expect(screen.getByText('Scopes')).toBeDefined();
    expect(screen.getByText('openid profile offline_access')).toBeDefined();
    expect(screen.getByText('Refresh token')).toBeDefined();
    expect(screen.getByText('Offline')).toBeDefined();
    expect(screen.getByText('Refresh expires')).toBeDefined();
    expect(screen.getByText('2027-01-15 10:05 UTC')).toBeDefined();
  });

  it('omits null fields instead of rendering placeholders', () => {
    render(
      <ServerDetails
        details={{
          has_token: true,
          expires_at: null,
          created_at: null,
          updated_at: null,
          scope: null,
          refresh_kind: 'unknown',
          refresh_expires_at: null
        }}
        now={NOW}
      />
    );
    expect(screen.queryByText('Signed in')).toBeNull();
    expect(screen.queryByText('Last refresh')).toBeNull();
    expect(screen.queryByText('Access token')).toBeNull();
    expect(screen.queryByText('Scopes')).toBeNull();
    expect(screen.queryByText('Refresh expires')).toBeNull();
    // The kind is always known (a three-literal enum, never null).
    expect(screen.getByText('Refresh token')).toBeDefined();
    expect(screen.getByText('Unknown')).toBeDefined();
  });

  it('reports an already expired access token honestly', () => {
    render(
      <ServerDetails details={{ ...FULL, expires_at: NOW - 5 }} now={NOW} />
    );
    expect(screen.getByText('expired')).toBeDefined();
  });

  it('labels a session-bound refresh token', () => {
    render(
      <ServerDetails details={{ ...FULL, refresh_kind: 'session' }} now={NOW} />
    );
    expect(screen.getByText('Session-bound')).toBeDefined();
  });
});
