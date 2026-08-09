/**
 * Tests for the per-server list item (`ServerItem`).
 *
 * Observable behavior only: the dot color per state (configured colors,
 * theme warn for expiring, neutral when indeterminate), the action set
 * per state (sign-in when invalid; details, verify, revoke otherwise),
 * the busy lock during a network action, and the detail fold.
 */

import { describe, expect, it, vi } from 'vitest';

import { fireEvent, render, screen } from '@testing-library/react';
import * as React from 'react';

import type { TokenStatusPayload } from '../api';
import type { DisplayStatus } from '../status';
import { ServerItem } from './ServerItem';

const NOW = 1_800_000_000;

const DESCRIPTOR = {
  name: 'stable-discovery',
  label: 'Stable (discovery)',
  color_valid: '#2e7d32',
  color_invalid: '#c62828'
};

const GREEN_DETAILS: TokenStatusPayload = {
  has_token: true,
  expires_at: NOW + 42,
  created_at: NOW - 3600,
  updated_at: NOW - 600,
  scope: 'openid',
  refresh_kind: 'session',
  refresh_expires_at: null
};

interface Callbacks {
  onLogin?: () => void;
  onVerify?: () => void;
  onRevoke?: () => void;
  onCopy?: () => void;
}

function renderItem(
  status: DisplayStatus,
  callbacks: Callbacks = {},
  options: { busy?: boolean; details?: TokenStatusPayload | null } = {}
): HTMLElement {
  const { container } = render(
    <ServerItem
      descriptor={DESCRIPTOR}
      status={status}
      details={options.details ?? (status === 'invalid' ? null : GREEN_DETAILS)}
      busy={options.busy ?? false}
      now={NOW}
      onLogin={callbacks.onLogin ?? (() => undefined)}
      onVerify={callbacks.onVerify ?? (() => undefined)}
      onRevoke={callbacks.onRevoke ?? (() => undefined)}
      onCopy={callbacks.onCopy ?? (() => undefined)}
    />
  );
  const item = container.querySelector('.tessera-ServerItem');
  expect(item).not.toBeNull();
  return item as HTMLElement;
}

function dotColor(item: HTMLElement): string {
  const dot = item.querySelector('.tessera-ServerItem-dot') as HTMLElement;
  expect(dot).not.toBeNull();
  return dot.style.getPropertyValue('--tessera-dot');
}

describe('ServerItem states', () => {
  it('paints the valid state with the configured valid color', () => {
    const item = renderItem('valid');
    expect(item.dataset.status).toBe('valid');
    expect(dotColor(item)).toBe('#2e7d32');
  });

  it('paints the invalid state with the configured invalid color', () => {
    const item = renderItem('invalid');
    expect(item.dataset.status).toBe('invalid');
    expect(dotColor(item)).toBe('#c62828');
  });

  it('paints the expiring state with the theme warn color', () => {
    const item = renderItem('expiring');
    expect(item.dataset.status).toBe('expiring');
    expect(dotColor(item)).toBe('var(--jp-warn-color1)');
  });

  it('keeps the indeterminate state neutral', () => {
    const item = renderItem('unknown', {}, { details: null });
    expect(item.dataset.status).toBe('unknown');
    expect(dotColor(item)).toBe('');
  });
});

describe('ServerItem actions per state', () => {
  it('offers sign-in only when invalid', () => {
    renderItem('invalid');
    expect(screen.getByRole('button', { name: /sign in/i })).toBeDefined();
    expect(screen.queryByRole('button', { name: /verify/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /revoke/i })).toBeNull();
  });

  it('offers details, verify, revoke, and copy when valid', () => {
    renderItem('valid');
    expect(screen.queryByRole('button', { name: /sign in/i })).toBeNull();
    expect(screen.getByRole('button', { name: /details/i })).toBeDefined();
    expect(screen.getByRole('button', { name: /verify/i })).toBeDefined();
    expect(screen.getByRole('button', { name: /revoke/i })).toBeDefined();
    expect(screen.getByRole('button', { name: /copy/i })).toBeDefined();
  });

  it('offers the same actions when expiring', () => {
    renderItem('expiring');
    expect(screen.getByRole('button', { name: /verify/i })).toBeDefined();
    expect(screen.getByRole('button', { name: /revoke/i })).toBeDefined();
    expect(screen.getByRole('button', { name: /copy/i })).toBeDefined();
  });

  it('offers no action while indeterminate', () => {
    renderItem('unknown', {}, { details: null });
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('fires the callbacks', () => {
    const onVerify = vi.fn();
    const onRevoke = vi.fn();
    renderItem('valid', { onVerify, onRevoke });
    screen.getByRole('button', { name: /verify/i }).click();
    screen.getByRole('button', { name: /revoke/i }).click();
    expect(onVerify).toHaveBeenCalledTimes(1);
    expect(onRevoke).toHaveBeenCalledTimes(1);
  });

  it('fires onCopy when the copy button is clicked', () => {
    const onCopy = vi.fn();
    renderItem('valid', { onCopy });
    screen.getByRole('button', { name: /copy/i }).click();
    expect(onCopy).toHaveBeenCalledTimes(1);
  });

  it('locks the network actions while busy', () => {
    renderItem('valid', {}, { busy: true });
    expect(
      (screen.getByRole('button', { name: /verify/i }) as HTMLButtonElement)
        .disabled
    ).toBe(true);
    expect(
      (screen.getByRole('button', { name: /revoke/i }) as HTMLButtonElement)
        .disabled
    ).toBe(true);
  });
});

describe('ServerItem details fold', () => {
  it('unfolds and folds the detail block', () => {
    const item = renderItem('valid');
    expect(item.querySelector('.tessera-ServerDetails')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /details/i }));
    expect(item.querySelector('.tessera-ServerDetails')).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /details/i }));
    expect(item.querySelector('.tessera-ServerDetails')).toBeNull();
  });
});
