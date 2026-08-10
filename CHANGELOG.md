# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-08-10

### Added

- Service observability: a greppable `[TESSERA ...]` log format on the
  `tessera.*` loggers, `TESSERA_LOG_LEVEL` control (DEBUG through
  CRITICAL), quieted third-party loggers, sanitized DEBUG lines at the
  flow boundaries (never a token, secret, code, or verifier), uncaught
  exceptions logged as method, bare path, and type only (so the `code`
  and `state` never reach an error log), and an unauthenticated liveness
  probe on the bare service prefix whose successful access line is logged
  at DEBUG so it stops flooding the logs with 404s (an error status there
  stays at INFO).
- Initial project scaffold: dual-stack layout (Python service and CLI, prebuilt
  TypeScript/React frontend), build tooling, and quality gates.
- Per-server OAuth configuration layer: validated YAML schema, client secret
  referenced by environment variable or file (never in cleartext), and the
  service public URL deriving the fixed callback URL.
- Encrypted multi-user token store: one refresh token per (username, server)
  pair, SQLCipher encryption at rest, key held outside the database.
- Authorization-code flow with PKCE (S256): one-shot, TTL-bound, user-bound
  anti-CSRF state, a bounded, self-evicting pending-flow store, and
  authorization endpoints held to a single acceptance rule before any
  redirect, whether they come from the configuration file or from the
  provider's discovery document (https required outside a loopback host).
- Access-token refresh: refresh-token rotation and reuse handling, with
  concurrent refreshes for one pair coalesced into a single provider call.
- Hub-authenticated Service endpoints (`/login`, `/callback`, `/token`,
  `/status`) and the runnable `python -m tessera.service` module.
- Keycloak test bench (multi-typology realms) and the development
  documentation section (architecture, environment, testing tiers).
- Project branding: logo in the README header and the documentation sidebar.
- End-to-end suite against the live Keycloak bench over pure HTTP
  (`pytest -m e2e`, opt-in, skipped with instructions when the bench is
  down).
- Panel-facing endpoints: `/servers` lists the declared buttons (name,
  label, state colors, declaration order) and `/status` reports the
  full stored-token detail (timestamps, granted scope, refresh kind, and
  the dated refresh-token expiry when the provider communicates one).
- Panel actions (XSRF-protected POST): `/verify` answers the provider's
  real verdict on the stored token (a definitive rejection purges it, a
  transient failure never fabricates an invalid), and `/revoke` notifies
  the provider best-effort (RFC 7009) then always purges locally, with
  an honest `provider_notified` flag.
- JupyterLab sidebar panel (skeleton): one live entry per declared
  server with its configured state colors, a 30-second status poll with
  an immediate refresh on tab return, a sign-in button opening the OAuth
  flow in a new tab, and sober indeterminate states when the service
  cannot be read.
- Panel actions and finishing touches: a foldable detail block per
  server (dates, scopes, refresh kind, informative access-token
  countdown), verify and revoke buttons with honest toasts and a native
  confirmation dialog, an opportunistic orange warning when the
  provider-dated refresh expiry enters its last quarter, the displayed
  panel version, and the full-color sidebar logo.
- Panel authentication via the page's API token (JupyterHub 5 refuses
  cookie sessions on AJAX): requires granting the browser's oauth
  session token the service access with
  `c.Spawner.oauth_client_allowed_scopes` (and the kernel-side
  environment token with `c.Spawner.server_token_scopes`), documented
  per credential and wired in the bench configuration; the panel points
  at the right directive when the grant is missing.

[Unreleased]: https://github.com/KaminoU/jupyterhub-tessera/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/KaminoU/jupyterhub-tessera/releases/tag/v1.0.0
