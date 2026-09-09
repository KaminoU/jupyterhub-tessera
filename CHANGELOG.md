# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.0] - 2026-09-09

### Added

- A pre-commit and CI check now compares the import roots of src/tessera
  against the declared dependencies. A second check installs the built
  distribution with its declared dependencies only and imports every
  module it ships, run locally through tox on every supported Python
  version and in CI against the artifact that gets published.
- Deployment guide: what a derived callback URL imposes when anything is
  interposed in front of the Hub, and the OAuth client requirements in a
  form that can be handed to whoever runs the identity provider.

### Changed

- URL validation is stricter, so a configuration that 1.0.0 loaded can
  now be refused: the service exits at startup instead of running with a
  value it cannot use. Three classes are affected, each with a direct
  remedy. A URL carrying any character outside printable ASCII is
  refused, so an internationalized host has to be written in its
  punycode `xn--` form. A userinfo component before the host is refused,
  so it has to be removed. A URL longer than 2048 characters is refused,
  so it has to be shortened. This covers the URLs of the configuration
  file, `service.public_url` and the provider endpoints, and the
  endpoints read from a provider's discovery document.
- kstlib floor raised to 3.7.1. Versions up to 3.7.0 imported
  typing_extensions at module level without declaring it, which made them
  unimportable on Python 3.13+ from a clean install. tessera was never
  affected: JupyterHub declares typing_extensions unconditionally through
  alembic, pydantic and sqlalchemy, so the module was always present. The
  floor moves so the guarantee comes from our own constraint rather than
  from a transitive one we do not control.

### Fixed

- URL validation now rejects an authority that cannot be one: a host
  carrying characters that cannot appear in a hostname (typically the
  residue of an unexpanded shell variable or a template placeholder), a
  host whose labels are malformed or oversized, a non-numeric port, and a
  userinfo component before the host. A malformed URL now raises the
  validator's own error instead of a bare parser exception. Such a
  `public_url` used to be reported as ok by `tessera doctor`, and the
  failure only surfaced in the browser; the configuration is now refused
  at load time with the field named. The same validator guards URLs read
  from a provider's discovery document.

### Security

- URL validation now bounds its input (length, printable ASCII) before
  parsing, so control characters can no longer survive into a URL derived
  from the configuration or from a provider's discovery document.

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

[Unreleased]: https://github.com/KaminoU/jupyterhub-tessera/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/KaminoU/jupyterhub-tessera/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/KaminoU/jupyterhub-tessera/releases/tag/v1.0.0
