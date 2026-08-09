# tessera.store

The encrypted, multi-user token store. One OAuth refresh token is kept per
(username, provider) pair in a single SQLCipher database encrypted at rest.
The store orchestrates `kstlib.db`; the encryption key is resolved from the
[store configuration](../guide/configuration.md) and is held by the running
service, never inside the database.

See the [configuration guide](../guide/configuration.md) for how the store
database location and its encryption key are declared.

## tessera.store.store

```{eval-rst}
.. automodule:: tessera.store.store
```

## tessera.store.models

```{eval-rst}
.. automodule:: tessera.store.models
```

## tessera.store.errors

```{eval-rst}
.. automodule:: tessera.store.errors
```
