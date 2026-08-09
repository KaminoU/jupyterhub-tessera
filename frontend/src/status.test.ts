/**
 * Tests for the pure status logic (`status.ts`).
 *
 * The expiry warning is opportunistic and honest: it fires only on a
 * provider-dated refresh expiry, at 25% of the granted window, and a
 * past date stays a warning (only a real verify can demote the color).
 * Formatting helpers are deterministic (UTC, no locale).
 */

import { describe, expect, it } from 'vitest';

import type { TokenStatusPayload } from './api';
import {
  EXPIRY_WARNING_RATIO,
  displayStatusOf,
  formatRemaining,
  formatTimestamp
} from './status';

const NOW = 1_800_000_000; // Unix seconds, arbitrary fixed point

function payload(overrides: Partial<TokenStatusPayload>): TokenStatusPayload {
  return {
    has_token: true,
    expires_at: null,
    created_at: NOW - 10_000,
    updated_at: NOW - 100,
    scope: 'openid',
    refresh_kind: 'session',
    refresh_expires_at: null,
    ...overrides
  };
}

describe('displayStatusOf', () => {
  it('is unknown without any payload', () => {
    expect(displayStatusOf(undefined, NOW)).toBe('unknown');
  });

  it('is unknown when the status could not be read', () => {
    expect(displayStatusOf('error', NOW)).toBe('unknown');
  });

  it('is invalid without a stored token', () => {
    expect(displayStatusOf(payload({ has_token: false }), NOW)).toBe('invalid');
  });

  it('is valid with a stored token and no dated refresh expiry', () => {
    expect(displayStatusOf(payload({}), NOW)).toBe('valid');
  });

  it('stays valid just above the warning threshold (short window)', () => {
    // Window 300 s, threshold 75 s: 80 s left is still comfortable.
    const status = payload({
      updated_at: NOW - 220,
      refresh_expires_at: NOW + 80
    });
    expect(displayStatusOf(status, NOW)).toBe('valid');
  });

  it('turns expiring just below the warning threshold (short window)', () => {
    // Window 300 s, threshold 75 s: 50 s left is inside the warning.
    const status = payload({
      updated_at: NOW - 250,
      refresh_expires_at: NOW + 50
    });
    expect(displayStatusOf(status, NOW)).toBe('expiring');
  });

  it('stays valid just above the warning threshold (long window)', () => {
    // Window 3600 s, threshold 900 s: 1000 s left is still comfortable.
    const status = payload({
      updated_at: NOW - 2600,
      refresh_expires_at: NOW + 1000
    });
    expect(displayStatusOf(status, NOW)).toBe('valid');
  });

  it('turns expiring just below the warning threshold (long window)', () => {
    // Window 3600 s, threshold 900 s: 800 s left is inside the warning.
    const status = payload({
      updated_at: NOW - 2800,
      refresh_expires_at: NOW + 800
    });
    expect(displayStatusOf(status, NOW)).toBe('expiring');
  });

  it('stays a warning once the dated expiry is past (verify decides)', () => {
    const status = payload({
      updated_at: NOW - 400,
      refresh_expires_at: NOW - 10
    });
    expect(displayStatusOf(status, NOW)).toBe('expiring');
  });

  it('never warns without the window start (updated_at unknown)', () => {
    const status = payload({
      updated_at: null,
      refresh_expires_at: NOW + 10
    });
    expect(displayStatusOf(status, NOW)).toBe('valid');
  });

  it('exposes the acted 25% ratio', () => {
    expect(EXPIRY_WARNING_RATIO).toBe(0.25);
  });
});

describe('formatTimestamp', () => {
  it('renders a stable UTC minute-precision stamp', () => {
    expect(formatTimestamp(0)).toBe('1970-01-01 00:00 UTC');
    expect(formatTimestamp(1_800_000_000)).toBe('2027-01-15 08:00 UTC');
  });
});

describe('formatRemaining', () => {
  it('counts seconds under a minute', () => {
    expect(formatRemaining(42)).toBe('in 42 s');
  });

  it('counts minutes under an hour', () => {
    expect(formatRemaining(300)).toBe('in 5 min');
  });

  it('counts hours and minutes above an hour', () => {
    expect(formatRemaining(7500)).toBe('in 2 h 5 min');
  });

  it('reports a past instant as expired', () => {
    expect(formatRemaining(-5)).toBe('expired');
    expect(formatRemaining(0)).toBe('expired');
  });
});
