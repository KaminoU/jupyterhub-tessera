# tessera.flow

The OAuth authorization-code flow layer. tessera owns the flow state so a
JupyterHub Service can run the flow for many concurrent users: crypto
generation of the anti-CSRF `state` and the PKCE (RFC 7636) verifier and
S256 challenge, a bounded, self-evicting store of in-flight flows keyed by
state, client secret resolution (the first real read of the secret
referenced by the configuration), and the orchestrator that ties them to
`kstlib.auth` for OIDC discovery and the back-channel code exchange.
Everything here is callable without tornado; the HTTP endpoints live in
the service layer.

## tessera.flow.orchestrator

```{eval-rst}
.. automodule:: tessera.flow.orchestrator
```

## tessera.flow.crypto

```{eval-rst}
.. automodule:: tessera.flow.crypto
```

## tessera.flow.models

```{eval-rst}
.. automodule:: tessera.flow.models
```

## tessera.flow.store

```{eval-rst}
.. automodule:: tessera.flow.store
```

## tessera.flow.secrets

```{eval-rst}
.. automodule:: tessera.flow.secrets
```

## tessera.flow.errors

```{eval-rst}
.. automodule:: tessera.flow.errors
```
