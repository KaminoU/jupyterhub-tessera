/**
 * Pure display logic of the panel: state derivation and formatting.
 *
 * @remarks
 * Everything here is honest by construction. The color derives from the
 * stored facts only: red for a constated absence or rejection, orange
 * only on a provider-dated refresh expiry entering its warning band
 * (never extrapolated), green otherwise, neutral when nothing reliable
 * is known. A past dated expiry stays a warning: only a real verify can
 * demote the color. Formatting is deterministic (UTC, no locale).
 */

import type { TokenStatusPayload } from './api';

/**
 * Visual state of a server entry.
 *
 * @remarks
 * `'valid'` paints the configured valid color (a refresh token is
 * stored, presumed valid), `'expiring'` the theme warn color (the
 * provider-dated refresh expiry entered its warning band), `'invalid'`
 * the configured invalid color (sign-in needed), and `'unknown'` is the
 * sober indeterminate state used before the first status answer or when
 * the service cannot be read: never a lying color.
 */
export type DisplayStatus = 'valid' | 'invalid' | 'expiring' | 'unknown';

/**
 * Fraction of the granted refresh window left that triggers the warning.
 *
 * @remarks
 * The window is `refresh_expires_at - updated_at`, both provider-dated
 * facts from the status payload, so the threshold adapts to any window
 * without configuration: 75 s on a 300 s session window, 900 s on a
 * one-hour offline idle window.
 */
export const EXPIRY_WARNING_RATIO = 0.25;

/**
 * Derive the visual state of a server from its polled status.
 *
 * @param status - The last polled payload, `'error'` when the poll
 *   failed, or undefined before the first answer.
 * @param now - The current Unix time in seconds.
 * @returns The visual state to paint.
 */
export function displayStatusOf(
  status: TokenStatusPayload | 'error' | undefined,
  now: number
): DisplayStatus {
  if (status === undefined || status === 'error') {
    return 'unknown';
  }
  if (!status.has_token) {
    return 'invalid';
  }
  if (status.refresh_expires_at !== null && status.updated_at !== null) {
    const window = status.refresh_expires_at - status.updated_at;
    const remaining = status.refresh_expires_at - now;
    if (window > 0 && remaining < window * EXPIRY_WARNING_RATIO) {
      return 'expiring';
    }
  }
  return 'valid';
}

/**
 * Format a Unix timestamp as a stable UTC stamp (minute precision).
 *
 * @param unixSeconds - The instant to format.
 */
export function formatTimestamp(unixSeconds: number): string {
  const iso = new Date(unixSeconds * 1000).toISOString();
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC`;
}

/**
 * Format a remaining duration for the informative countdown.
 *
 * @param seconds - Seconds until the instant; zero or less is expired.
 */
export function formatRemaining(seconds: number): string {
  if (seconds <= 0) {
    return 'expired';
  }
  if (seconds < 60) {
    return `in ${Math.floor(seconds)} s`;
  }
  if (seconds < 3600) {
    return `in ${Math.floor(seconds / 60)} min`;
  }
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return `in ${hours} h ${minutes} min`;
}
