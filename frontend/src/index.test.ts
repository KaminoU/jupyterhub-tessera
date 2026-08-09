/**
 * Tests for the plugin entry point: identity, activation, and the
 * registration of the panel widget in the right sidebar.
 */

import { describe, expect, it, vi } from 'vitest';

import type { JupyterFrontEnd } from '@jupyterlab/application';
import { Widget } from '@lumino/widgets';

import { tesseraIcon } from './icon';
import plugin from './index';

describe('tessera plugin', () => {
  it('has the expected id', () => {
    expect(plugin.id).toBe('tessera:plugin');
  });

  it('is marked for automatic activation', () => {
    expect(plugin.autoStart).toBe(true);
  });

  it('registers the panel widget in the right sidebar', () => {
    const add = vi.fn();
    const app = { shell: { add } } as unknown as JupyterFrontEnd;
    plugin.activate(app);
    expect(add).toHaveBeenCalledTimes(1);
    const [widget, area, options] = add.mock.calls[0] as [
      Widget,
      string,
      { rank: number }
    ];
    expect(widget).toBeInstanceOf(Widget);
    expect(widget.id).toBe('tessera-panel');
    expect(widget.title.icon).toBe(tesseraIcon);
    expect(widget.title.caption.toLowerCase()).toContain('tessera');
    expect(area).toBe('right');
    expect(options).toEqual({ rank: 900 });
  });
});
