/**
 * Anti-drift test for the displayed panel version.
 *
 * `PANEL_VERSION` is a constant because the tsc build cannot import
 * `package.json` from outside its rootDir (TS6059); this test pins the
 * constant to the single npm source of truth, so a release bump that
 * forgets the constant fails red instead of shipping a stale display.
 */

import { describe, expect, it } from 'vitest';

import packageJson from '../../package.json';

import { PANEL_VERSION } from './version';

describe('PANEL_VERSION', () => {
  it('matches the package.json version (single source of truth)', () => {
    expect(PANEL_VERSION).toBe(packageJson.version);
  });
});
