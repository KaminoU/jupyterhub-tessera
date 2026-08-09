/**
 * Tests for the sidebar icon: the full-color project logo, inlined.
 */

import { describe, expect, it } from 'vitest';

import { tesseraIcon } from './icon';

describe('tesseraIcon', () => {
  it('is registered under the tessera namespace', () => {
    expect(tesseraIcon.name).toBe('tessera:icon');
  });

  it('inlines the complete colored logo (plate, motif, emblem)', () => {
    expect(tesseraIcon.svgstr).toContain('#254B9E'); // hexagon plate
    expect(tesseraIcon.svgstr).toContain('#F4EFF0'); // circuit motif
    expect(tesseraIcon.svgstr).toContain('#FAA919'); // central emblem
  });

  it('keeps the source viewBox', () => {
    expect(tesseraIcon.svgstr).toContain('viewBox="0 0 766.896 861.826"');
  });
});
