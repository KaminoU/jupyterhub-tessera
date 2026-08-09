# tessera.config

Server configuration layer: the schema, the loader, and the typed errors. A
YAML file declares one entry per OAuth server (keyed by name); tessera reads
it through `kstlib.config` and validates it into frozen dataclasses. Secrets
are referenced by environment variable or file path, never embedded in the
file.

See the [configuration guide](../guide/configuration.md) for an annotated
example and the field reference.

## tessera.config.models

```{eval-rst}
.. automodule:: tessera.config.models
```

## tessera.config.loader

```{eval-rst}
.. automodule:: tessera.config.loader
```

## tessera.config.errors

```{eval-rst}
.. automodule:: tessera.config.errors
```
