# Keycloak test infrastructure

The `infra/` directory ships a self-contained Keycloak container used as the
identity provider for tier 3 validation (a real JupyterHub talking to a real
OAuth2/OIDC provider) and for the end-to-end test suite. The realms are
imported declaratively at startup: no manual admin-console clicking is needed
to get a working bench.

## Quick start

```bash
cd infra
docker compose up -d     # first start takes ~30s (realm import)
docker compose ps        # wait for the container to report healthy
jupyterhub -f jupyterhub_config.py   # full bench: Hub + tessera service
docker compose down      # stop and discard runtime state
```

The Hub configuration works from any working directory
(`jupyterhub -f infra/jupyterhub_config.py`); see
[Pointing tessera at the bench](#pointing-tessera-at-the-bench).

The bench token store is disposable: after upgrading the installed wheel
across a schema change, delete `tessera-bench.db` and restart the Hub
(there is deliberately no migration code before the first release).

The admin console is served at `http://localhost:8008` (credentials `admin` /
`admin`). The default port is 8008 so this bench can run next to the
upstream kstlib bench, which keeps the usual 8080; set `KEYCLOAK_PORT` to
remap it if 8008 is taken.

## Realms: the rotation typologies

Refresh-token rotation is a realm-level setting in Keycloak (Realm Settings,
Tokens, "Revoke Refresh Token"), so the bench pre-configures two realms that
differ only in that setting:

| Realm              | Refresh token rotation                                                                                      | Discovery endpoint                                                               |
| ------------------ | ----------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `tessera-stable`   | OFF: a refresh token stays valid until it expires                                                           | `http://localhost:8008/realms/tessera-stable/.well-known/openid-configuration`   |
| `tessera-rotating` | ON (revoke on use, max reuse 0): every refresh returns a new refresh token and invalidates the previous one | `http://localhost:8008/realms/tessera-rotating/.well-known/openid-configuration` |

`tessera-rotating` is the realm that exercises rotation handling and reuse
detection; `tessera-stable` is the plain baseline.

## Clients: the PKCE typologies

Each realm defines the same two confidential clients (client secret:
`test-only-not-a-secret`, a public test fixture):

| Client                  | PKCE                                                                                           |
| ----------------------- | ---------------------------------------------------------------------------------------------- |
| `tessera-pkce-required` | Enforced by the provider (S256): an authorization request without a code challenge is rejected |
| `tessera-pkce-optional` | Accepted but not required                                                                      |

tessera always sends PKCE; the optional client verifies that this does not
depend on the provider enforcing it. Both clients register the exact redirect
URIs `http://localhost:8000/services/tessera/callback` and
`http://127.0.0.1:8000/services/tessera/callback` (no wildcards), matching the
callback the service derives from `public_url`. Direct access grants are
enabled so end-to-end tests can seed tokens without driving a browser.

The remaining typology axes need no extra realm or client: OIDC discovery
versus explicit endpoints, and scopes with or without `offline_access`
(available as an optional client scope), are both selected in the tessera
`servers.yml` against the same realms.

## Token lifespans are aggressive on purpose

The realms use deliberately short lifespans so that expiry and refresh
behavior can be tested without waiting:

| Setting                      | Value  |
| ---------------------------- | ------ |
| Access token lifespan        | 60 s   |
| SSO session idle timeout     | 300 s  |
| SSO session max lifespan     | 3600 s |
| Offline session idle timeout | 3600 s |

When exploring manually, expect the tessera button to turn red quickly: that
is the bench doing its job (a 60-second access token and a 5-minute idle
session force the refresh and expiry paths to run all the time), not a bug.

## Test users

Each realm defines the same users. Every value below is a public test
fixture, not a secret.

| User            | Password              | Role                                                                                                            |
| --------------- | --------------------- | --------------------------------------------------------------------------------------------------------------- |
| `alice`         | `alice-test-password` | regular user                                                                                                    |
| `bob`           | `bob-test-password`   | regular user                                                                                                    |
| `tessera-admin` | `admin-test-password` | `realm-admin` (client role of `realm-management`): administers its own realm without the master console account |

Two regular users allow testing per-user token isolation in the store and
simultaneous flows from distinct sessions.

Every user carries the realm's default composite role
(`default-roles-<realm>`), which includes `offline_access`. Declaratively
imported users do not receive it automatically (unlike users created in
the admin console); without it Keycloak refuses to deliver offline tokens
and the code exchange fails with `not_allowed`.

## Pointing tessera at the bench

The bench ships its own tessera configuration, `infra/servers.yml`. Its
three servers select the typology axes against the realms above: OIDC
discovery versus explicit endpoints, offline versus session refresh
tokens (`offline_access` requested or not), and the stable versus
rotating realm. Rotation is a realm-level setting, orthogonal to the
endpoint mode, so these three cover every axis:

```{literalinclude} ../../../../infra/servers.yml
:language: yaml
:caption: infra/servers.yml
```

`infra/jupyterhub_config.py` runs the whole bench: a loopback Hub with
throwaway authentication (any username and password), and tessera as a
Hub-managed service pointed at this file. It injects the public bench
fixtures (`TESSERA_DB_KEY`, `TESSERA_CLIENT_SECRET`) and grants users the
`access:services!service=tessera` scope, which no user holds by default.
A tier-1 test loads `infra/servers.yml` on every run, so the bench
configuration cannot drift from the documented realms.
