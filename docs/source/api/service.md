# tessera.service

JupyterHub Service layer: the tornado application exposing the OAuth flow
over HTTP, and the runnable module that assembles it from the environment
JupyterHub provides. Identity always comes from JupyterHub authentication,
never from a request parameter, and no state, code, token, or secret value
is ever echoed in a response or a log line.

The endpoints, all under the JupyterHub service prefix:

| Route             | Method | Credentials                    | Success                                      |
| ----------------- | ------ | ------------------------------ | -------------------------------------------- |
| `/login`          | GET    | Hub OAuth cookie or token      | 302 redirect to the identity provider        |
| `/callback`       | GET    | Hub OAuth cookie               | 200, static confirmation page (token stored) |
| `/token`          | GET    | API token only, never a cookie | JSON `access_token`, `expires_at`, `scope`   |
| `/status`         | GET    | Hub OAuth cookie or token      | JSON stored-token status (fields below)      |
| `/servers`        | GET    | Hub OAuth cookie or token      | JSON `servers`: the declared button list     |
| `/verify`         | POST   | Cookie (XSRF) or token         | JSON `valid`: the provider's real verdict    |
| `/revoke`         | POST   | Cookie (XSRF) or token         | JSON `revoked`, `provider_notified`          |
| `/oauth_callback` | GET    | (Hub handshake)                | Completes the browser login with the Hub     |

`/servers` is the static topology the frontend loads once: one entry per
declared server, in declaration order, each carrying `name`, `label`,
`color_valid`, and `color_invalid` from the validated configuration, and
nothing else (no client id, no endpoint, no secret).

`/status` is the cheap per-server detail the frontend polls; all fields
come from one store read and no token value ever appears:

| Field                | Meaning                                                                      |
| -------------------- | ---------------------------------------------------------------------------- |
| `has_token`          | A refresh token is stored for the caller and this server                     |
| `expires_at`         | Access-token expiry seen at the last store write (indicative)                |
| `created_at`         | First successful sign-in for this pair (Unix seconds)                        |
| `updated_at`         | Last store write (moves on every successful refresh)                         |
| `scope`              | Granted scope string, when the provider communicated one                     |
| `refresh_kind`       | `offline`, `session`, or `unknown`, derived from the scope                   |
| `refresh_expires_at` | Dated refresh-token expiry when communicated, else null (never extrapolated) |

Without a stored token, `has_token` is `false`, `refresh_kind` is
`unknown`, and every other field is null.

`/verify` is the panel's one reliable validity test: it runs the same
refresh round-trip as `/token` but only the verdict leaves. A definitive
provider rejection answers `{"valid": false}` (the stored record is
purged, the next status poll turns red); a transient provider failure is
a 502 (`refresh_unavailable`), never a false verdict. On providers that
rotate refresh tokens, a successful verify consumes a real refresh and
the rotated token is persisted; concurrent calls coalesce into one
provider round-trip.

`/revoke` notifies the provider first (best-effort, RFC 7009), then
always purges the stored record. `provider_notified` is honest: `true`
only when the provider actually confirmed the revocation; `false` when
no revocation endpoint is known (explicit-endpoints servers, or a
provider without one) or the call failed, in which case the local purge
still happened. Revoking again answers the 409 `no_stored_token`. The call
targets the stored refresh token alone, with the matching token type hint,
since that is the only token tessera holds. An access token already handed
to a kernel is therefore out of its reach: the next `get_token` fails with
the 409 `no_stored_token`, but a value a notebook already holds stays
usable until it expires, unless the provider chooses to invalidate it along
with the grant.

Both actions are POST-only and XSRF-protected for cookie sessions: send
the standard `X-XSRFToken` header matching the Hub-signed `_xsrf` cookie
(JupyterLab's default fetch behavior). Token-authenticated calls are
exempt, per JupyterHub semantics.

`/token` is token-only by construction: its authenticator is a plain
`HubAuth`, which cannot read browser cookies, so a browser session alone
can never extract a token. Failures follow one mapping: 400 for invalid or
expired material, 404 for an undeclared server, 409 as an actionable JSON
error when no usable token is stored (`no_stored_token`,
`refresh_rejected`), 502 when the provider fails (the callback page
distinguishes a definitive provider rejection, a 4xx answer, from an
interaction that could not be completed: an unreachable provider, a
failed discovery, a completed discovery whose authorization endpoint is
missing or is not an acceptable URL, or a 5xx answer worth retrying), 503
when too many sign-ins are in flight, and 500 for a service
misconfiguration.

Reaching the service requires the `access:services!service=<name>`
scope on THREE distinct credentials, each governed by its own Hub
directive. This is the classic deployment trap: granting one of them
does nothing for the other two.

| Credential                                                       | Used by                                | Directive                               |
| ---------------------------------------------------------------- | -------------------------------------- | --------------------------------------- |
| The user (browser navigation: `/login`, `/callback`)             | The sign-in flow                       | `c.JupyterHub.load_roles`               |
| The page token (the browser's oauth session token, `PageConfig`) | The JupyterLab panel (status, actions) | `c.Spawner.oauth_client_allowed_scopes` |
| The environment token (`$JUPYTERHUB_API_TOKEN`)                  | Kernel-side clients calling `/token`   | `c.Spawner.server_token_scopes`         |

JupyterHub 5 refuses cookie sessions on non-navigation requests, so
tokens are the only workable credentials for the panel and the kernel.
A complete configuration grants all three:

```python
c.JupyterHub.load_roles = [
    {"name": "user", "scopes": ["self", "access:services!service=tessera"]},
]
# Page token: ADDITIONAL scopes, server access always included.
c.Spawner.oauth_client_allowed_scopes = [
    "access:services!service=tessera",
]
# Environment token: REPLACES the default `server` role scopes, so they
# are listed explicitly alongside the service access.
c.Spawner.server_token_scopes = [
    "users:activity!user",
    "access:servers!server",
    "access:services!service=tessera",
]
```

A token only ever receives the subset of these scopes held by the user,
so the `load_roles` grant is required in every case. Token scopes are
frozen at token creation: after changing these directives, restart the
Hub and the single-user servers (and sign in again) so fresh tokens are
issued.

## Logging and health

Logging is controlled by `TESSERA_LOG_LEVEL` (one of `DEBUG`, `INFO`,
`WARNING`, `ERROR`, `CRITICAL`; `INFO` by default, an unsupported value
falling back to `INFO`). The `tessera.*` loggers emit a greppable line
format, and the chatty third-party loggers (the HTTP client, the
kstlib config loader, and the auth library) are quieted to `WARNING`:

```text
[TESSERA ::: 2026-07-12 16:30:51,123 ::: INFO] tessera.service.app: 200 GET /services/tessera/status 1.42ms
```

No token, secret, authorization code, PKCE verifier, or personal data is
ever written to a log, at any level, including `DEBUG`. Access lines are
stripped of their query string (OAuth material travels there), and an
uncaught exception is logged as its method, bare path, and type only, so
the `code` and `state` never reach an error log or a stack frame either.

The bare service prefix serves an unauthenticated liveness probe:

| Route      | Method | Auth | Success                                       |
| ---------- | ------ | ---- | --------------------------------------------- |
| `{prefix}` | GET    | none | 200, `{"service": "tessera", "status": "ok"}` |

It exists so the Hub/proxy liveness poll gets a 200 instead of a 404
every interval, and a successful probe's access line is logged at `DEBUG`
so that poll stays out of the way; an error status on the prefix (a bad
method, a scan burst) stays at `INFO`, like every real endpoint. The body
is minimal by design: no version, no configuration, no secret.

## tessera.service.app

```{eval-rst}
.. automodule:: tessera.service.app
```

## tessera.service.handlers

```{eval-rst}
.. automodule:: tessera.service.handlers
```

## tessera.service.runner

```{eval-rst}
.. automodule:: tessera.service.runner
```
