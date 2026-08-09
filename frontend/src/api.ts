/**
 * Typed data layer between the panel and the tessera service.
 *
 * @remarks
 * The service lives under the JupyterHub origin (`/services/tessera/`),
 * not under the single-user server, so every URL is derived from the
 * page's hub configuration. Requests authenticate with the page's own
 * JupyterHub API token (the standard `Authorization: token` header) and
 * deliberately carry no cookie: JupyterHub 5 refuses cookie sessions on
 * non-navigation requests without an XSRF proof the panel cannot read,
 * while token calls are exempt by design, so the token is the single
 * deterministic credential. It stays in memory, is never stored, never
 * logged, and never displayed. Responses are validated structurally
 * before use: a non-JSON body (a login page at the end of a redirect
 * chain) or a malformed payload rejects, and the panel renders an
 * indeterminate state instead of trusting it. No OAuth token value ever
 * transits here: the service only exposes booleans and metadata.
 */

import { PageConfig } from '@jupyterlab/coreutils';

/**
 * Error raised by the data layer, carrying the HTTP diagnosis.
 *
 * @remarks
 * `status` is the HTTP status of a non-ok response (403 lets the panel
 * point at the missing RBAC grant, 502 at an unreachable provider), or
 * null when the failure is structural (network, non-JSON, malformed
 * payload). The message never contains a token or a received value.
 */
export class ServiceError extends Error {
  /** HTTP status of the failed response, or null for structural failures. */
  readonly status: number | null;

  /**
   * Build a service error.
   *
   * @param message - A static, value-free description.
   * @param status - The HTTP status, when one was received.
   */
  constructor(message: string, status: number | null = null) {
    super(message);
    this.name = 'ServiceError';
    this.status = status;
  }
}

/**
 * Kind of refresh token the service holds, derived from its scope.
 */
export type RefreshKind = 'offline' | 'session' | 'unknown';

/**
 * One declared server, as served by `GET /servers`.
 *
 * @remarks
 * Field names mirror the service wire contract (snake_case JSON keys).
 */
export interface ServerDescriptor {
  /** Declared server name, the key for every other endpoint. */
  name: string;
  /** Display label of the button. */
  label: string;
  /** Button color when a stored token is presumed valid. */
  color_valid: string;
  /** Button color when no usable token is stored. */
  color_invalid: string;
}

/**
 * Stored-token status for one server, as served by `GET /status`.
 *
 * @remarks
 * Field names mirror the service wire contract (snake_case JSON keys).
 * `has_token` is the button's truth; everything else is display detail.
 */
export interface TokenStatusPayload {
  /** Whether a refresh token is stored (and presumed valid). */
  has_token: boolean;
  /** Indicative access-token expiry (Unix seconds), if known. */
  expires_at: number | null;
  /** First successful sign-in for this server (Unix seconds). */
  created_at: number | null;
  /** Last store write; moves on every successful refresh. */
  updated_at: number | null;
  /** Granted scope string, when the provider communicated one. */
  scope: string | null;
  /** Kind of refresh token, derived from the stored scope. */
  refresh_kind: RefreshKind;
  /** Dated refresh-token expiry when communicated, never extrapolated. */
  refresh_expires_at: number | null;
}

const REFRESH_KINDS: readonly string[] = ['offline', 'session', 'unknown'];

/**
 * Return the tessera service base URL (with a trailing slash).
 *
 * @remarks
 * Derived from the JupyterHub page configuration: the hub prefix minus
 * its `hub/` tail is the deployment base (`/` or `/jupyter/`), and the
 * hub host covers subdomain deployments. Outside a hub page the root
 * `/services/tessera/` is assumed.
 */
export function serviceBaseUrl(): string {
  const hubHost = PageConfig.getOption('hubHost');
  const hubPrefix = PageConfig.getOption('hubPrefix') || '/';
  const base = hubPrefix.replace(/hub\/$/, '');
  return `${hubHost}${base}services/tessera/`;
}

/**
 * Return the sign-in URL for a server, to open in a new tab.
 *
 * @param server - The declared server name.
 */
export function loginUrl(server: string): string {
  return `${serviceBaseUrl()}login?server=${encodeURIComponent(server)}`;
}

/**
 * Fetch the declared servers, in declaration order.
 *
 * @param signal - Optional abort signal wired to the caller's lifecycle.
 * @returns The declared button descriptors.
 * @throws ServiceError when the response is not ok, not JSON, or
 *   malformed; a 403 status diagnoses a missing RBAC grant.
 */
export async function fetchServers(
  signal?: AbortSignal
): Promise<ServerDescriptor[]> {
  const payload = await requestJson(`${serviceBaseUrl()}servers`, signal);
  if (!isServersPayload(payload)) {
    throw new ServiceError('tessera: malformed servers payload');
  }
  return payload.servers;
}

/**
 * Fetch the stored-token status of one server.
 *
 * @param server - The declared server name.
 * @param signal - Optional abort signal wired to the caller's lifecycle.
 * @returns The status payload (booleans and metadata only, never a token).
 * @throws ServiceError when the response is not ok, not JSON, or malformed.
 */
export async function fetchStatus(
  server: string,
  signal?: AbortSignal
): Promise<TokenStatusPayload> {
  const url = `${serviceBaseUrl()}status?server=${encodeURIComponent(server)}`;
  const payload = await requestJson(url, signal);
  if (!isTokenStatusPayload(payload)) {
    throw new ServiceError('tessera: malformed status payload');
  }
  return payload;
}

/**
 * Verify the stored token against the provider, for real.
 *
 * @param server - The declared server name.
 * @param signal - Optional abort signal wired to the caller's lifecycle.
 * @returns The provider's verdict; false means the record was purged.
 * @throws ServiceError when no verdict was reached (502: unreachable).
 */
export async function postVerify(
  server: string,
  signal?: AbortSignal
): Promise<{ valid: boolean }> {
  const url = `${serviceBaseUrl()}verify?server=${encodeURIComponent(server)}`;
  const payload = await requestJson(url, signal, 'POST');
  if (!isVerifyPayload(payload)) {
    throw new ServiceError('tessera: malformed verify payload');
  }
  return payload;
}

/**
 * Revoke the stored token: provider best-effort, local purge always.
 *
 * @param server - The declared server name.
 * @param signal - Optional abort signal wired to the caller's lifecycle.
 * @returns The honest outcome, including whether the provider confirmed.
 * @throws ServiceError when the request failed (409: nothing to revoke).
 */
export async function postRevoke(
  server: string,
  signal?: AbortSignal
): Promise<{ revoked: boolean; provider_notified: boolean }> {
  const url = `${serviceBaseUrl()}revoke?server=${encodeURIComponent(server)}`;
  const payload = await requestJson(url, signal, 'POST');
  if (!isRevokePayload(payload)) {
    throw new ServiceError('tessera: malformed revoke payload');
  }
  return payload;
}

/** Authentication headers: the page's API token, when the page has one. */
function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  const token = PageConfig.getToken();
  if (token) {
    headers.Authorization = `token ${token}`;
  }
  return headers;
}

/** Call a service URL with the API token and return validated JSON. */
async function requestJson(
  url: string,
  signal?: AbortSignal,
  method: 'GET' | 'POST' = 'GET'
): Promise<unknown> {
  const response = await fetch(url, {
    method,
    credentials: 'omit',
    headers: authHeaders(),
    signal
  });
  if (!response.ok) {
    throw new ServiceError(
      `tessera: service request failed (${response.status})`,
      response.status
    );
  }
  const contentType = response.headers.get('Content-Type') ?? '';
  if (!contentType.includes('application/json')) {
    // A login page at the end of the redirect chain: the caller renders
    // an indeterminate state instead of trusting it.
    throw new ServiceError('tessera: unexpected non-JSON service response');
  }
  return response.json() as Promise<unknown>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isNumberOrNull(value: unknown): value is number | null {
  return value === null || typeof value === 'number';
}

function isServerDescriptor(value: unknown): value is ServerDescriptor {
  return (
    isRecord(value) &&
    typeof value.name === 'string' &&
    typeof value.label === 'string' &&
    typeof value.color_valid === 'string' &&
    typeof value.color_invalid === 'string'
  );
}

function isServersPayload(
  value: unknown
): value is { servers: ServerDescriptor[] } {
  return (
    isRecord(value) &&
    Array.isArray(value.servers) &&
    value.servers.every(isServerDescriptor)
  );
}

function isTokenStatusPayload(value: unknown): value is TokenStatusPayload {
  return (
    isRecord(value) &&
    typeof value.has_token === 'boolean' &&
    isNumberOrNull(value.expires_at) &&
    isNumberOrNull(value.created_at) &&
    isNumberOrNull(value.updated_at) &&
    (value.scope === null || typeof value.scope === 'string') &&
    typeof value.refresh_kind === 'string' &&
    REFRESH_KINDS.includes(value.refresh_kind) &&
    isNumberOrNull(value.refresh_expires_at)
  );
}

function isVerifyPayload(value: unknown): value is { valid: boolean } {
  return isRecord(value) && typeof value.valid === 'boolean';
}

function isRevokePayload(
  value: unknown
): value is { revoked: boolean; provider_notified: boolean } {
  return (
    isRecord(value) &&
    typeof value.revoked === 'boolean' &&
    typeof value.provider_notified === 'boolean'
  );
}
