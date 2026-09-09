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

The rest of the lifecycle, stopping the bench itself and the other files a
run leaves behind, is in
[Stopping and purging the bench](#stopping-and-purging-the-bench).

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

## Stopping and purging the bench

`docker compose down` stops Keycloak, and that is one process out of four.
The Hub, the proxy it starts and the Hub-managed tessera service are three
more, and they are stopped the way any deployment is stopped: SIGTERM to
the Hub, never `kill -9`, then a check that the ports came back. The
procedure, the port check and the way to find an orphan are in
{doc}`/guide/deployment`, under "Stopping and restarting cleanly".

Two of those ports are the bench's own: the tessera service is declared on
10101 in `infra/jupyterhub_config.py`, and Keycloak answers on 8008. The
three others are JupyterHub defaults that the bench never overrides, and
the port table of that same guide names them and says which process holds
each one.

### The launch directory decides where the bench writes

The Hub configuration resolves its own paths, so it can be started from any
directory. The files a run produces do not follow it: they are created in
the working directory of the process, and there are four of them.

| File                       | Written by | What deleting it costs                                   |
| -------------------------- | ---------- | -------------------------------------------------------- |
| `tessera-bench.db`         | tessera    | every stored token: sign in again on each server         |
| `jupyterhub.sqlite`        | the Hub    | every issued token and all Hub-side user state           |
| `jupyterhub_cookie_secret` | the Hub    | the open browser sessions, regenerated at the next start |
| `jupyterhub-proxy.pid`     | the proxy  | nothing, it is rewritten at the next start               |

The quick start above changes into `infra/` first, so the four land there;
starting from the repository root, as the configuration docstring shows,
drops them at the top of the tree instead. Both places are ignored by git,
so the only symptom of alternating between the two is two sets of files and
a bench that looks empty when it is not.

Of the four, only `tessera-bench.db` is a routine reset, and only across a
schema change. The other three are rarely the answer:

- A changed RBAC directive needs no purge. Roles and role assignments are
  reconciled from the configuration at every Hub start, so restarting the
  Hub is what applies them. What a restart does not change is a token
  already issued: signing in again, and restarting the single-user server,
  is what hands out a new one. Which credential carries which scope is in
  {doc}`/api/service`.
- Keycloak needs no `-v`. The compose file declares no named volume and the
  development-mode database lives inside the container, so `docker compose
down` already discards the runtime state, and the next `up -d` re-imports
  the realms.

### Inspecting the token store

The store is a SQLCipher database, encrypted with the key the Hub
configuration injects as `TESSERA_DB_KEY`. A SQLCipher 4 client opens it
with that key and nothing else:

```python
from sqlcipher3 import dbapi2 as sqlcipher

conn = sqlcipher.connect("tessera-bench.db")
conn.execute("PRAGMA key = 'KEY'")  # the TESSERA_DB_KEY value of the bench
print(conn.execute("SELECT name FROM sqlite_master").fetchall())
```

A client configured for the older parameter generation fails on that same
file even with the right key, and says nothing about why:

```text
>>> conn.execute("PRAGMA cipher_compatibility = 3")   # issued after the key
>>> conn.execute("SELECT name FROM sqlite_master")
sqlcipher3.dbapi2.DatabaseError: file is not a database
```

That message does not discriminate: a wrong key produces exactly the same
error on the same file. The reason reaches the process standard error only,
as `hmac check failed for pgno=1`. The cure is `PRAGMA
cipher_compatibility = 4` on a fresh connection, issued after `PRAGMA key`,
since a cipher pragma set before the key has no effect at all.

Those parameters belong to the SQLCipher build that is linked, not to
tessera: neither tessera nor kstlib sets a cipher pragma, only `PRAGMA
key`. Measured on 2026-09-09 in the project environment, the `sqlcipher3`
binding 2.6.0 on engine 4.12.0 community reports a page size of 4096,
`kdf_iter` 256000, HMAC-SHA512 and PBKDF2-HMAC-SHA512. Read them from your
own build rather than trusting that list, and see
{doc}`/guide/configuration` for how a real deployment holds the key.
