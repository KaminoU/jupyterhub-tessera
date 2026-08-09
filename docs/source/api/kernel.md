# tessera.kernel

Notebook-side client for tessera access tokens. A kernel imports it (or
loads it as an IPython extension) to obtain a fresh OAuth access token from
the running JupyterHub service, over plain HTTP, authenticating with the
single-user server's own `$JUPYTERHUB_API_TOKEN`. It is deliberately
synchronous (a cell runs in a synchronous context) and never imports the
async service-side code, so it stays importable in a bare kernel.

Two names are exposed:

| Name                        | Use                                                             |
| --------------------------- | --------------------------------------------------------------- |
| `get_token("<server>")`     | Return the current access token for a declared server, a string |
| `TESSERA_TOKEN["<server>"]` | The same value through a lazy mapping, convenient inside a cell |

Load the extension in a notebook, then read a token:

```python
%load_ext tessera.kernel

token = get_token("my-idp")
# or, equivalently:
token = TESSERA_TOKEN["my-idp"]
```

The service base URL is resolved from the environment, highest priority
first: `TESSERA_URL`, otherwise derived from `JUPYTERHUB_PUBLIC_URL` and
`JUPYTERHUB_BASE_URL`. Each call is a fresh request and nothing is cached,
so a token revoked at the provider is never served from a stale value. No
token value is ever cached, logged, or placed in an error message: a
failure raises `TesseraTokenError` with an actionable, token-free message.
For the deployment topologies (public URL versus loopback) and a diagnosis
grid for transport errors, see {doc}`/guide/notebook`.

Reaching `/token` requires the environment token to carry the
`access:services!service=tessera` scope, granted with
`c.Spawner.server_token_scopes` (see {doc}`/api/service`). Token scopes are
frozen at token creation, so restart the single-user servers after changing
that directive.

## tessera.kernel.client

```{eval-rst}
.. automodule:: tessera.kernel.client
```
