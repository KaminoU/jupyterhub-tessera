/**
 * Vitest environment shims for the JupyterLab widget stack under jsdom.
 *
 * These are import-time requirements of the JupyterLab dependency tree,
 * not of tessera's own code: `@lumino/dragdrop` references `DragEvent`,
 * and `@jupyterlab/ui-components` (through the Jupyter design system)
 * queries `matchMedia` and observes with `ResizeObserver`. jsdom
 * implements none of them; inert stand-ins are enough, as no test
 * exercises drag-and-drop, media queries, or layout observation.
 */

if (typeof globalThis.DragEvent === 'undefined') {
  class DragEventShim extends Event {}
  globalThis.DragEvent = DragEventShim as unknown as typeof DragEvent;
}

if (typeof globalThis.matchMedia === 'undefined') {
  globalThis.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false
  })) as unknown as typeof globalThis.matchMedia;
}

if (typeof globalThis.ResizeObserver === 'undefined') {
  class ResizeObserverShim {
    observe(): void {
      // intentionally inert
    }
    unobserve(): void {
      // intentionally inert
    }
    disconnect(): void {
      // intentionally inert
    }
  }
  globalThis.ResizeObserver =
    ResizeObserverShim as unknown as typeof ResizeObserver;
}

export {};
