# Accessing the token from a notebook

Once a server's button is green (a valid refresh token is stored), code
running in your single-user server can read a fresh OAuth access token
through the `tessera.kernel` extension. The token is fetched from the
tessera service on demand and never stored in the notebook.

## Reading a token

Load the extension, then read the token for a declared server (the name is
the one shown in the panel):

```python
%load_ext tessera.kernel

token = get_token("my-idp")
```

`TESSERA_TOKEN` is an equivalent, mapping-style accessor:

```python
token = TESSERA_TOKEN["my-idp"]
```

Both perform one request to the service and return the current access
token as a string. Nothing is cached in the kernel: every access is a
fresh request, so a token revoked at the provider is never served from a
stale value. Use the token immediately (for example in an `Authorization`
header); do not keep it in a variable that outlives the cell, and never
print or log it.

If no token is stored yet, or it can no longer be refreshed, the call
raises `TesseraTokenError` with an actionable message (sign in again with
the tessera button). See {doc}`/api/kernel` for the full API and the error
mapping.

## Loading the extension automatically

To avoid a manual `%load_ext` in every notebook, enable the extension in an
IPython configuration file. The one line that does it is:

```python
c.InteractiveShellApp.extensions.append("tessera.kernel")
```

`tessera install-kernel-config` writes exactly this file for you. `--target`
is the IPython **config directory**; the file written is always
`<target>/ipython_config.py`. Which directory to pick depends on who owns
the setup, and on whether you own `/etc`:

- **Machine-wide**: `/etc/ipython`, the default. Every kernel on the host
  auto-loads the extension. Requires root.
- **Per environment, no root**: `{sys.prefix}/etc/ipython`, where
  `sys.prefix` is the kernel environment root. The config travels with the
  environment and needs no write access outside it, which makes it the
  natural choice for a single-user server image (a Docker image, for
  example) and the only option when the host filesystem is not yours to
  write.
- **Single user**: `~/.ipython/profile_default`, to enable it for one user
  only.

Installing into the current environment, without root:

```console
$ tessera install-kernel-config --target "$(python -c 'import sys; print(sys.prefix)')/etc/ipython"
# expected: a success panel reporting
#   Written  <sys.prefix>/etc/ipython/ipython_config.py
#   Next     restart single-user servers / kernels to load it
```

That last line is not decoration. An IPython config is read when a kernel
starts, so **kernels already running keep the previous behavior**: restart
them, or the single-user servers, before concluding that the auto-load does
not work.

Running the command a second time on the same target is expected to refuse:

```console
$ tessera install-kernel-config --target /etc/ipython
# expected: an error panel, exit status 1
#   refusing to overwrite /etc/ipython/ipython_config.py
```

That is the no-clobber guarantee working, not an obstacle to route around:
the file is created with an exclusive open, so tessera can never overwrite a
configuration you or another tool put there. Edit the file by hand when it
needs to change.

`get_token` and `TESSERA_TOKEN` are then available in every notebook and
console started there. The repository ships this snippet as
`infra/ipython_config.py`. An environment where `tessera` is not installed
logs a warning and starts the kernel normally, so a machine-wide file is
safe to deploy broadly.

## Locating the service

The client resolves the service base URL from the single-user server's
environment, highest priority first:

1. `TESSERA_URL`, when set (`c.Spawner.environment` is where a deployment
   injects it).
2. Otherwise a value derived from `JUPYTERHUB_PUBLIC_URL` and
   `JUPYTERHUB_BASE_URL`, which JupyterHub sets when
   `c.JupyterHub.public_url` is configured.

Whichever value wins, the request authenticates with the server's own
`$JUPYTERHUB_API_TOKEN`, which must carry the
`access:services!service=tessera` scope (granted with
`c.Spawner.server_token_scopes`; see {doc}`/api/service`). That credential
is a Hub token rather than a browser session, so it is accepted the same way
whichever network path reaches the service. The choice below is therefore
about the network path only, never about permissions.

### Public URL, the default

`tessera config-snippet` emits the public form:

```python
c.Spawner.environment = {
    "TESSERA_URL": "https://hub.example.org/services/tessera/",
}
```

It works in every layout, including kernels running on other hosts than the
service, which is why it is the default. The price is TLS: the call leaves
the kernel over https, so the Hub certificate must be verifiable **by the
trust store of the kernel environment**, not only by your browser. A
certificate signed by an internal CA needs that CA installed wherever the
kernels run.

### Loopback, recommended when kernels and service share a host

When the single-user servers run on the same host as the service, point the
client straight at the bind address from your service block:

```python
c.Spawner.environment = {
    "TESSERA_URL": "http://127.0.0.1:10101/services/tessera/",
}
```

The service mounts its routes under the service prefix on its bind address
too, so the path stays `/services/tessera/`. There is no certificate to
verify and no proxy hop, and the Hub token authenticates exactly as before.

| Topology   | Works when                           | Cost                                                         |
| ---------- | ------------------------------------ | ------------------------------------------------------------ |
| Public URL | always, kernels on any host          | the certificate must be verifiable by the kernel trust store |
| Loopback   | kernels and service on the same host | not usable once kernels run elsewhere                        |

```{warning}
Never answer a certificate problem by turning verification off. The client
offers no such switch, by design. Install the issuing CA in the kernel
environment, or use the loopback topology.
```

## Diagnosing a TesseraTokenError

Every failure raises `TesseraTokenError` with a message that carries no
token. When the service could not be reached at all, the message ends with
the underlying exception class, and that class is the diagnosis:

```text
my-idp: could not reach the tessera service (ConnectError)
```

| Symptom                    | Cause                                                            | Remedy                                                     |
| -------------------------- | ---------------------------------------------------------------- | ---------------------------------------------------------- |
| `(ConnectError)`           | the kernel cannot verify the certificate served at `TESSERA_URL` | install the issuing CA, or switch to loopback              |
| `(ConnectTimeout)`         | a proxy intercepts the call, loopback included                   | exclude the service host through `no_proxy`                |
| `NameError: TESSERA_TOKEN` | the extension is not loaded in this kernel                       | `%load_ext tessera.kernel`, or import the names explicitly |

### ConnectError: the certificate is not verifiable

Reproduce the handshake alone, from the kernel environment, using nothing
but the standard library:

```console
$ python -c "import urllib.request; urllib.request.urlopen('https://hub.example.org/services/tessera/')"
# expected: no ssl.SSLCertVerificationError
#           (an HTTP error status here is fine, the handshake is what matters)
```

An `SSLCertVerificationError` means the trust store of that environment does
not know the issuing CA. Install the CA there, or move to the loopback
topology when the kernels sit on the service host. Do not disable
verification.

### ConnectTimeout: a proxy is in the way

The client honors the proxy environment variables (`http_proxy`,
`https_proxy`, `no_proxy`, in either case), so a proxy declared for the
single-user server is used even for a request to `127.0.0.1`. When that
proxy cannot route to the service, the call gives up after ten seconds.

```console
$ python -c "import os; print({k: v for k, v in os.environ.items() if 'proxy' in k.lower()})"
# expected: a no_proxy value covering the service host, for example
#           {'http_proxy': '...', 'no_proxy': '127.0.0.1,localhost'}
```

Fix it in the spawner environment, extending whatever the deployment
already sets rather than replacing it:

```python
import os

_no_proxy = os.environ.get("no_proxy", "")
c.Spawner.environment = {
    "TESSERA_URL": "http://127.0.0.1:10101/services/tessera/",
    "no_proxy": ",".join(part for part in (_no_proxy, "127.0.0.1,localhost") if part),
}
```

### NameError: the extension is not loaded

`NameError: name 'TESSERA_TOKEN' is not defined` is not a tessera error at
all: the name was never injected into that kernel. Check the package is
importable there, then load it:

```console
$ python -c "import tessera.kernel; print('importable')"
# expected: importable
```

```python
%load_ext tessera.kernel
```

Import the names directly when you prefer not to depend on the extension
machinery:

```python
from tessera.kernel import TESSERA_TOKEN, get_token
```

To stop loading it by hand in every notebook, see
[Loading the extension automatically](#loading-the-extension-automatically).
