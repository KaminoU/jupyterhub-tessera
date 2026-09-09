/**
 * The panel version displayed in the sidebar header.
 *
 * @remarks
 * A constant, because the tsc build cannot import `package.json` from
 * outside its rootDir (TS6059). It is pinned to the npm source of truth
 * by a test, and the npm version is itself pinned to the Python one by
 * the release checklist, so the displayed value also diagnoses the
 * service: a mismatch on the bench means a stale installed wheel.
 */
export const PANEL_VERSION = '1.1.0';
