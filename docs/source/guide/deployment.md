# Deploying tessera

This page takes a JupyterHub administrator from an empty deployment to a
working per-server sign-in button. It covers the sequence and the Hub
wiring. The configuration file itself has its own page: see
{doc}`configuration` for every field it accepts.

```{important}
tessera does not create OAuth clients and does not replace your Hub's
authenticator. It consumes an identity provider you already run.

Before starting, you need an OAuth2 or OIDC provider (Keycloak, Okta,
Auth0, SAS Viya, or any other) on which you can register a confidential
client and obtain a client ID and a client secret. If you do not have
one yet, set that up first: nothing below will work without it.
```

## What you are about to build

Three pieces cooperate:

- A **Hub-managed service** that runs the OAuth flow and holds the
  encrypted token store. It is the only component that ever sees a token.
- A **JupyterLab panel** showing one button per declared server, green
  when a valid refresh token is stored, red otherwise.
- A **kernel client** exposing the access token inside notebooks.

Each of the three reaches the service with a different credential, which
is why step 5 grants three separate scopes and step 6 explains them.

## Prerequisites

The three pieces above do not run in the same environment and do not need
the same things. Check the three tiers separately: tier 1 is a hard
requirement, tier 2 decides what users see, tier 3 only matters where
notebooks read tokens.

### Tier 1: the service host

JupyterHub 4 or 5 on Python 3.10 or newer. tessera runs inside the Hub's
own environment as a Hub-managed service, so this tier is not
negotiable: without it there is no OAuth flow and no token store.

```console
$ jupyterhub --version
# expected: 4.x or 5.x

$ python -c "import sys; print(sys.version_info >= (3, 10))"
# expected: True
```

### Tier 2: the JupyterLab panel

JupyterLab 4 and the `jupyterhub-tessera` package, both in the
**single-user environment**, which in most deployments is not the
environment from tier 1. The extension ships prebuilt inside the wheel,
so installing the package is the whole install.

```console
$ jupyter labextension list
# expected: tessera vX.Y.Z enabled OK (python, jupyterhub-tessera)
```

A single-user environment without the package is a degraded deployment,
not a broken one: the service runs, and users sign in by opening the
login URL directly.

```text
<public_url>/services/tessera/login?server=NAME
```

What is lost is the button and its green/red status, never the token.
Installing the package in that environment later switches the panel on
with no change to the Hub configuration, so tier 1 first and tier 2
afterwards is a legitimate rollout order rather than a failure.

### Tier 3: kernel access

Python 3.10 or newer in the kernel environment. JupyterLab is irrelevant
here: `tessera.kernel` speaks plain HTTP to the service, it is not a
browser component.

```console
$ python -c "import tessera.kernel; print('ok')"
# expected: ok
```

Where a kernel environment is older and cannot be upgraded, notebooks can
still reach the service directly. The endpoint authenticates with the
single-user server's own Hub token and answers JSON:

```python
import os

import requests

url = os.environ["TESSERA_URL"] + "token?server=my-idp"
headers = {"Authorization": "token " + os.environ["JUPYTERHUB_API_TOKEN"]}
token = requests.get(url, headers=headers, timeout=10).json()["access_token"]
```

`TESSERA_URL` reaches the single-user server through the configuration
block of step 5. See {doc}`notebook` for the supported client, which adds
error mapping and keeps nothing in the kernel.

## 1. Install

```console
$ pip install jupyterhub-tessera
```

Install it in the environment that runs the Hub. The distribution is
named `jupyterhub-tessera`; the Python package imports as `tessera`. The
JupyterLab extension ships prebuilt inside the wheel, so there is no
separate frontend install and no `npm` step.

Check what you got:

```console
$ tessera info
```

## 2. Decide your public URL and derive the callback

tessera builds exactly one callback URL for the whole deployment:

```text
<public_url>/services/tessera/callback
```

where `public_url` is the base URL your users reach the Hub at. It is
deliberately not configurable on its own, and it is never built from
request input. Write it down now: you need the exact string in step 3.

## 3. Register the OAuth client at your provider

On your identity provider, create a **confidential** client (one with a
client secret) for tessera, and register the callback URL from step 2 as
its redirect URI. The value must match exactly, including the scheme and
any trailing path.

tessera uses the authorization-code flow with PKCE (S256). Grant the
client the scopes your users need; `openid` is required, and
`offline_access` is what makes a provider issue a refresh token that
survives the browser session. Without it, users have to sign in again far
more often.

Keep the client ID and the client secret: the next step references them.

## 4. Write the configuration file

```console
$ tessera init-config --output /etc/tessera/servers.yml
```

The wizard asks for the provider, the client ID, the scopes, and how the
client secret is referenced, then writes a file that reloads cleanly. It
never writes a secret value, only a reference to one.

See {doc}`configuration` for the full field reference, the discovery
versus explicit provider modes, and how the token store is encrypted.

## 5. Declare tessera as a Hub service

```console
$ tessera config-snippet
```

This prints a block to paste into your `jupyterhub_config.py`. It
declares the service, grants the three scopes described below, and points
the single-user servers at the service. The service name is fixed to
`tessera` because the callback path depends on it.

```{tip}
`tessera config-snippet --raw` emits the same block with placeholder
values and no prompts, so you can pipe it straight into a file.
```

## 6. Understand the three scopes you just granted

The block from step 5 already carries three RBAC grants, and this is the
step deployments get wrong: the failure is silent, every request answers
`403` with nothing obviously misconfigured. The cause is that three
different credentials reach the service, and granting one does nothing
for the other two:

- **The user's role** (`c.JupyterHub.load_roles`) lets a signed-in user
  reach the service at all.
- **The page token** (`c.Spawner.oauth_client_allowed_scopes`) is what
  the JupyterLab panel uses from the browser.
- **The server token** (`c.Spawner.server_token_scopes`) lives in the
  single-user server's environment and is what the kernel client uses.

```{warning}
`c.Spawner.server_token_scopes` **replaces** the default list rather than
extending it. Keep `users:activity!user` and `access:servers!server` in
your value, or the single-user server loses abilities unrelated to
tessera.
```

For the full breakdown of which credential does what, and why a token
only ever holds the subset of scopes its user holds, see
{doc}`/api/service`.

## 7. Provide the secrets

Two secrets reach the service through its environment, never through the
configuration file:

- The **client secret** of the OAuth client from step 3.
- The **store encryption key**, which encrypts the refresh tokens at
  rest. Hold it somewhere other than the database file: a key sitting
  next to the data it protects protects nothing.

The service block from step 5 shows where to inject them. See
{doc}`configuration` for the supported reference methods and the trade
offs between them.

## 8. Auto-load the kernel client (optional)

If you want `TESSERA_TOKEN` available in notebooks without users typing
`%load_ext`, deploy the IPython configuration:

```console
$ tessera install-kernel-config
```

It writes into `/etc/ipython` by default and refuses to overwrite an
existing file. Use `--target` for a different configuration directory,
for example `{sys.prefix}/etc/ipython` in a virtual environment. See
{doc}`notebook` for what users then get.

## 9. Verify

Before restarting anything, check the configuration end to end:

```console
$ tessera doctor
```

It loads the configuration and, for each declared server, probes the OIDC
discovery document and checks that the client-secret reference resolves.
It is read-only, prints no secret value, and exits non-zero on failure,
so it works in CI.

Useful variants:

| Command                         | What it does                                   |
| ------------------------------- | ---------------------------------------------- |
| `tessera doctor --offline`      | Skips every network check                      |
| `tessera doctor --server X`     | Checks one server only                         |
| `tessera doctor --verbose`      | Shows the endpoints discovered at the provider |
| `tessera doctor --list-servers` | Lists declared servers and exits               |

Then restart the Hub, open JupyterLab, and click a button. A successful
sign-in turns it green.

## Stopping and restarting cleanly

The Hub is not the only process in the deployment. It starts the proxy
(`configurable-http-proxy`) and, because tessera is Hub-managed, the
tessera service too. Both are separate operating-system processes that
can outlive the Hub, and an orphan keeps its port and keeps serving the
configuration it read when it started.

Stop the Hub with SIGTERM: `Ctrl-C` in a foreground terminal, the stop
command of your process manager, or

```console
$ pkill -TERM -f jupyterhub
```

```{warning}
Never `kill -9` the Hub. SIGKILL gives it no chance to stop the proxy and
the managed service, which is exactly how orphans are created. tessera
handles SIGTERM: it stops accepting connections and closes the token
store before exiting.
```

Before starting again, confirm the four ports of the deployment are free.
They belong to three different processes:

| Port      | Bound by | Configured with                      | Default |
| --------- | -------- | ------------------------------------ | ------- |
| Public    | proxy    | `c.JupyterHub.port`                  | 8000    |
| Hub API   | Hub      | `c.JupyterHub.hub_port`              | 8081    |
| Proxy API | proxy    | `c.ConfigurableHTTPProxy.api_url`    | 8001    |
| Service   | tessera  | the `url` field of the service block | none    |

```console
$ ss -tlnp | grep -E ':(8000|8081|8001|10101)\b'
# expected: no output at all (substitute your own service port for 10101)
```

Anything still listening is an orphan from the previous run. Terminate it
by name rather than by guessing from the port, and check again:

```console
$ pkill -TERM -f 'tessera.service'
$ pgrep -af 'tessera.service'
# expected: no output
```

Starting the Hub on top of an orphan is the most common reason a
configuration change appears to do nothing: the port is already held by a
process running the previous configuration.

## Troubleshooting

Find the symptom, run the control, apply the remedy. Every control below
is read-only and prints no secret value.

| Symptom                                           | Control                                              | Remedy                                                                                                                        |
| ------------------------------------------------- | ---------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| Every request answers `403`                       | `grep -n 'access:services' jupyterhub_config.py`     | [Everything answers 403](#everything-answers-403)                                                                             |
| The provider rejects the callback URL             | `tessera doctor --server NAME`                       | [The provider rejects the callback](#the-provider-rejects-the-callback)                                                       |
| The sign-in URL carries a variable name           | `tessera doctor --server NAME`                       | [The sign-in URL contains an unexpanded variable](#the-sign-in-url-contains-an-unexpanded-variable)                           |
| Signing in returns a `502`                        | `tessera doctor --server NAME --verbose`             | [Signing in returns a 502](#signing-in-returns-a-502)                                                                         |
| The panel cannot reach the service                | `curl -sS <bind_url>/services/tessera/`              | [The panel says it cannot reach the service](#the-panel-says-it-cannot-reach-the-service)                                     |
| Sign-in fails just after the provider             | `tessera doctor --server NAME`                       | [The sign-in fails right after authenticating at the provider](#the-sign-in-fails-right-after-authenticating-at-the-provider) |
| A configuration change changes nothing            | `pgrep -af 'tessera.service'`                        | [Configuration changes have no effect after a restart](#configuration-changes-have-no-effect-after-a-restart)                 |
| `tessera doctor` says the provider is unreachable | `curl -sS <issuer>/.well-known/openid-configuration` | [tessera doctor reports the provider unreachable](#tessera-doctor-reports-the-provider-unreachable)                           |

### Everything answers 403

A scope is missing. Work out which credential is refused: the panel
failing to load its server list points at
`c.Spawner.oauth_client_allowed_scopes` or at the `user` role, while a
notebook failing to read a token points at `c.Spawner.server_token_scopes`.

```console
$ grep -n 'access:services' jupyterhub_config.py
# expected: three matches, one per grant (load_roles,
#           oauth_client_allowed_scopes, server_token_scopes)
```

Fewer than three matches names the missing grant. Re-read step 6 and
compare all three against your configuration.

### The provider rejects the callback

The redirect URI registered at the provider does not match
`<public_url>/services/tessera/callback` exactly.

```console
$ tessera doctor --server my-idp
# expected: redirect_uri  ok  https://hub.example.org/services/tessera/callback
```

Compare that value with the one registered at the provider, character by
character, including the scheme and any Hub base path.

### The sign-in URL contains an unexpanded variable

You click the button, the provider answers with an error page, and the
address bar shows a variable name where your host should be, url-encoded:
`redirect_uri=https%3A%2F%2F%24HUB_URL%2Fservices%2F...`.

tessera never expands shell variables in its configuration file. A file
written through an unquoted heredoc, or a templating step that did not
run, keeps the text verbatim, and validation accepts it because
`$HUB_URL` parses as a host name.

```console
$ grep -n public_url /etc/tessera/servers.yml
# expected: public_url: https://hub.example.org
# wrong:    public_url: $HUB_URL   (or ${HUB_URL})

$ tessera doctor --server my-idp
# expected: redirect_uri  ok  https://hub.example.org/services/tessera/callback
```

Rewrite the file with literal values (`tessera init-config --output
/etc/tessera/servers.yml --force` writes a clean one), confirm with
`tessera doctor`, then restart the service. The configuration is read
once at startup, so editing the file changes nothing until the service
restarts: see [Stopping and restarting
cleanly](#stopping-and-restarting-cleanly).

### Signing in returns a 502

The service could not obtain a usable authorization endpoint for that
server. The log names the server and which of three cases applies:

```text
discovery for server 'NAME' returned no usable authorization_endpoint
discovery for server 'NAME' announced an unusable authorization_endpoint, rejected
discovery for server 'NAME' announced a non-TLS authorization_endpoint, rejected
```

```console
$ tessera doctor --server my-idp --verbose
# expected: well-known  ok  https://idp.example.com/.well-known/openid-configuration
#           followed by the discovered endpoints
```

The remedy depends on which line you got. `returned no usable
authorization_endpoint` means the document does not advertise the field at
all: confirm it carries `authorization_endpoint`, and if the provider does
not expose discovery at all, declare the endpoints explicitly instead, see
{doc}`configuration`. `announced an unusable authorization_endpoint` means
the field is present but is not an absolute http(s) URL with a host, a
relative path for instance: the provider's document is malformed and has to
be fixed there. `announced a non-TLS authorization_endpoint` means the field
is an `http://` URL on a non-loopback host, and tessera refuses to send a
browser through a cleartext authorization step: serve the provider over
https, or, for a local bench only, publish it on a loopback host
(`localhost`, `127.0.0.1`, `::1`), which stays accepted.

The rejected value never appears in the log, since it comes from the
network: read it from the document itself with the command above.

### The panel says it cannot reach the service

The service is not answering behind the Hub proxy. Probe its liveness
route, which needs no authentication, on the bind URL from your service
block:

```console
$ curl -sS http://127.0.0.1:10101/services/tessera/
# expected: {"service": "tessera", "status": "ok"}
```

An answer here but not through the Hub means the proxy route is wrong:
confirm the bind URL in the service block is the one the Hub proxies to.
No answer at all means the service is not running: check the Hub log for
its startup line, which reports the address, the prefix, and the number
of declared servers.

### The sign-in fails right after authenticating at the provider

You authenticate at the provider, come back, and the sign-in ends in an
error instead of a green button. The exchange succeeded but the provider
issued no refresh token, and tessera has nothing to store.

```console
$ tessera doctor --server my-idp
# expected: scopes  ok  openid offline_access ...
```

Add `offline_access` to that server's scopes when it is absent, then
restart the service. When it is already there, check whether the provider
needs a specific setting to issue offline (long-lived) tokens.

### Configuration changes have no effect after a restart

You edit the configuration, restart, and the deployment behaves exactly
as before. An orphaned service from the previous run is still listening
and still serving the configuration it read at its own startup.

```console
$ pgrep -af 'tessera.service'
# expected: exactly one line, the service the running Hub spawned

$ ss -tlnp | grep -E ':(8000|10101)\b'
# expected: one listener per port (substitute your own ports)
```

Two lines from `pgrep`, or a listener that the current run does not own,
means an orphan is answering instead of your new configuration. Terminate
it with SIGTERM and restart: see [Stopping and restarting
cleanly](#stopping-and-restarting-cleanly).

### `tessera doctor` reports the provider unreachable

Network path, firewall, or a typo in the issuer URL.

```console
$ curl -sS https://idp.example.com/.well-known/openid-configuration
# expected: a JSON document carrying authorization_endpoint and token_endpoint
```

Run it from the Hub host itself, not from your workstation: the two do
not necessarily share a route to the provider. When the document is
served but `tessera doctor` still fails, compare the issuer in your
configuration against the URL you just fetched, using `tessera doctor
--verbose` to see the value tessera actually probes.
