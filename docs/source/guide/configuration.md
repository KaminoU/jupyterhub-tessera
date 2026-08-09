# Configuring OAuth servers

tessera reads a single YAML file that declares the OAuth servers your users
can authenticate against. Each server becomes one button in JupyterLab. This
page shows where the file lives, its shape, and every field it accepts.

## Where the file lives

tessera resolves the configuration path with a short cascade, highest
priority first:

1. An explicit path passed in code (`load_config("/path/servers.yml")`).
2. The `TESSERA_CONFIG` environment variable.
3. The default location `~/.config/tessera/servers.yml`.

The first source that is set wins. An empty `TESSERA_CONFIG` is ignored and
the default location is used.

## File shape

The file has three top-level sections: `service` (the deployment-wide
settings), `store` (the encrypted token store, covered below), and `servers`.
Under `servers`, each key is a server name (the
identifier shown in URLs and used by the button) and each value is that
server's settings. Naming a server twice is impossible because the keys form
a mapping, so there is no ambiguity about which entry wins.

```{literalinclude} ../../../examples/servers.yml
:language: yaml
:caption: servers.yml
```

## The service section

The `service` section carries the deployment-wide settings.

`public_url`
: The public base URL your users reach the Hub at (scheme and host, plus the
Hub base path if it is served under one). `https` is required; plain `http`
is accepted only for a loopback host during local development.

The OAuth callback URL is derived from it as
`<public_url>/services/tessera/callback` and is exposed as
`config.service.redirect_uri`. It is deliberately not configurable on its
own: one deployment has exactly one callback URL, and it can never be built
from request input. Register that exact URL as the redirect URI when you
create the OAuth client at each provider.

## The token store

The `store` section configures where refresh tokens are persisted and how the
database is encrypted. tessera stores tokens in a single SQLCipher database
(encrypted at rest as a whole), keyed by (user, provider).

`db_location`
: Path to the database file.

The encryption key is referenced by exactly one of the following. Setting
none, or more than one, is rejected.

`key_file`
: Path to a file holding the key, readable only by the service account
(mode `0400` recommended). This is the recommended default.

`key_env`
: Name of an environment variable holding the key.

`key_sops`
: Path to a SOPS-encrypted file holding the key (with an optional
`key_sops_key` naming the entry inside it).

```{note}
tessera reads the store encryption key from, at your choice, a plain file
(`0400`), an environment variable, or a SOPS file. Facing an attacker with
machine access (the service account), the three are equivalent: machine
access is game over (assumed threat model). They differ only if the key leaks
without machine access (an over-broad backup): a SOPS file backed by a KMS or
an off-host age key stays unusable, unlike a plain file, an environment
variable captured in a dump, or a SOPS file with a local age key. Keep the key
out of the database backups, whichever method you use.
```

## Choosing the provider mode

Each server declares its provider in exactly one of two ways. Setting both,
or neither, is rejected.

Discovery mode (recommended when the provider supports OIDC)
: Give a single `issuer` URL. tessera lets the provider advertise its
endpoints through `.well-known/openid-configuration`.

Explicit mode
: Give both `authorization_endpoint` and `token_endpoint`. Use this when the
provider does not expose discovery, or when you must pin exact endpoints.

All URLs must use `https`. Plain `http` is accepted only for a loopback host
(`localhost`, `127.0.0.1`, `::1`), which keeps a local development provider
usable without weakening production configurations.

## Referencing the client secret

tessera never stores a secret in this file. Reference it by exactly one of:

`client_secret_env`
: The name of an environment variable that holds the secret.

`client_secret_file`
: The path to a file (readable only by the service account, typically mode 0600) that holds the secret.

Setting both, setting neither, or embedding a cleartext `client_secret` key
is rejected. A cleartext secret is refused with a dedicated error and a
security log entry, and its value is never echoed back.

The secret is read at its first real use (the first login or callback for
that server) and then held in memory for the lifetime of the service
process. Rotating a client secret therefore takes effect after a service
restart.

## Field reference

| Field                             | Required                      | Default     | Notes                                   |
| --------------------------------- | ----------------------------- | ----------- | --------------------------------------- |
| `provider.issuer`                 | one mode required             | none        | Discovery mode.                         |
| `provider.authorization_endpoint` | with `token_endpoint`         | none        | Explicit mode.                          |
| `provider.token_endpoint`         | with `authorization_endpoint` | none        | Explicit mode.                          |
| `client_id`                       | yes                           | none        | Public client identifier.               |
| `client_secret_env`               | one reference required        | none        | Env var name holding the secret.        |
| `client_secret_file`              | one reference required        | none        | Path to a file holding the secret.      |
| `scopes`                          | yes                           | none        | Non-empty list of scope strings.        |
| `button.label`                    | no                            | server name | Text shown on the button.               |
| `button.color_valid`              | no                            | `#2e7d32`   | Color when a valid token exists.        |
| `button.color_invalid`            | no                            | `#c62828`   | Color when no valid token exists.       |
| `auto_refresh`                    | no                            | `true`      | Refresh the access token automatically. |

## Errors you may see

`ConfigFileError`
: The file is missing, too large, or not valid YAML.

`ConfigValidationError`
: The content parsed but violates the schema (a missing field, a bad URL, an
empty `servers` mapping, and similar).

`SecretConfigError`
: A secret is referenced incorrectly, or a cleartext secret was found. This
is a subclass of `ConfigValidationError`.
