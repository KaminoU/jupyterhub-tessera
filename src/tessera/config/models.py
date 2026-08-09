"""Typed, validated configuration objects for tessera OAuth servers.

These frozen dataclasses are pure value objects: once built they are
always valid. Parsing and validation of untrusted mapping data happens in
the ``from_mapping`` classmethods, which raise typed errors from
:mod:`tessera.config.errors`. Secret values are never read here: a server
only references a secret by environment variable or by file path.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from tessera._shared.urls import UrlValidationError, validate_url
from tessera.config.errors import ConfigValidationError, SecretConfigError

log = logging.getLogger(__name__)

DEFAULT_COLOR_VALID = "#2e7d32"
DEFAULT_COLOR_INVALID = "#c62828"
DEFAULT_AUTO_REFRESH = True


def _validate_url(raw: object, field: str, owner: str) -> str:
    """Validate that ``raw`` is an acceptable https (or loopback http) URL.

    The rule itself lives in :mod:`tessera._shared.urls`, shared with the
    OAuth flow so a URL read from a provider's discovery document meets the
    same bar as one written here by the operator. This wrapper only names
    the offending field in the message.

    Args:
        raw: The candidate value from the configuration.
        field: The dotted field name, for error messages.
        owner: The owning context (for example ``"server 'demo'"`` or
            ``"service"``), for error messages.

    Returns:
        The stripped, validated URL string.

    Raises:
        ConfigValidationError: If the value is not an acceptable URL.
    """
    try:
        return validate_url(raw)
    except UrlValidationError as exc:
        raise ConfigValidationError(f"{owner}: '{field}' {exc.detail}") from exc


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """OAuth provider location: an OIDC discovery issuer or explicit endpoints."""

    issuer: str | None
    authorization_endpoint: str | None
    token_endpoint: str | None

    @property
    def uses_discovery(self) -> bool:
        """Whether the provider is configured via an OIDC issuer (discovery)."""
        return self.issuer is not None

    @classmethod
    def from_mapping(cls, raw: object, server: str) -> ProviderConfig:
        """Validate a raw provider mapping into a ProviderConfig.

        Exactly one mode must be used: an ``issuer`` (OIDC discovery) or an
        explicit ``authorization_endpoint`` plus ``token_endpoint``.

        Args:
            raw: The raw provider value from the configuration.
            server: The server name owning the block, for error messages.

        Returns:
            The validated provider configuration.

        Raises:
            ConfigValidationError: If the provider block is invalid.
        """
        if not isinstance(raw, Mapping):
            raise ConfigValidationError(f"server '{server}': 'provider' must be a mapping")
        issuer = raw.get("issuer")
        authorization = raw.get("authorization_endpoint")
        token = raw.get("token_endpoint")
        has_issuer = issuer is not None
        has_endpoints = authorization is not None or token is not None
        if has_issuer and has_endpoints:
            raise ConfigValidationError(
                f"server '{server}': 'provider' must set either 'issuer' "
                "(discovery) or explicit endpoints, not both"
            )
        if not has_issuer and not has_endpoints:
            raise ConfigValidationError(
                f"server '{server}': 'provider' must set 'issuer' or both "
                "'authorization_endpoint' and 'token_endpoint'"
            )
        if has_issuer:
            return cls(
                issuer=_validate_url(issuer, "provider.issuer", f"server '{server}'"),
                authorization_endpoint=None,
                token_endpoint=None,
            )
        if authorization is None or token is None:
            raise ConfigValidationError(
                f"server '{server}': explicit provider requires both "
                "'authorization_endpoint' and 'token_endpoint'"
            )
        return cls(
            issuer=None,
            authorization_endpoint=_validate_url(
                authorization, "provider.authorization_endpoint", f"server '{server}'"
            ),
            token_endpoint=_validate_url(token, "provider.token_endpoint", f"server '{server}'"),
        )


@dataclass(frozen=True, slots=True)
class ButtonConfig:
    """Per-server button display: label and valid/invalid state colors."""

    label: str
    color_valid: str
    color_invalid: str

    @classmethod
    def from_mapping(cls, raw: object, server: str) -> ButtonConfig:
        """Validate a raw button mapping, filling defaults from the server name.

        Args:
            raw: The raw button value, or None when the block is absent.
            server: The server name, used as the default label.

        Returns:
            The validated button configuration.

        Raises:
            ConfigValidationError: If a provided button field is invalid.
        """
        if raw is None:
            return cls(
                label=server,
                color_valid=DEFAULT_COLOR_VALID,
                color_invalid=DEFAULT_COLOR_INVALID,
            )
        if not isinstance(raw, Mapping):
            raise ConfigValidationError(f"server '{server}': 'button' must be a mapping")
        label = raw.get("label", server)
        color_valid = raw.get("color_valid", DEFAULT_COLOR_VALID)
        color_invalid = raw.get("color_invalid", DEFAULT_COLOR_INVALID)
        for name, value in (
            ("label", label),
            ("color_valid", color_valid),
            ("color_invalid", color_invalid),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ConfigValidationError(
                    f"server '{server}': 'button.{name}' must be a non-empty string"
                )
        return cls(
            label=label.strip(),
            color_valid=color_valid.strip(),
            color_invalid=color_invalid.strip(),
        )


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Validated configuration for a single OAuth server."""

    name: str
    provider: ProviderConfig
    client_id: str
    client_secret_env: str | None
    client_secret_file: str | None
    scopes: tuple[str, ...]
    button: ButtonConfig
    auto_refresh: bool

    @classmethod
    def from_mapping(cls, name: str, raw: object) -> ServerConfig:
        """Validate a raw server entry into a ServerConfig.

        Args:
            name: The server name (the key under ``servers`` in the file).
            raw: The raw server mapping.

        Returns:
            The validated server configuration.

        Raises:
            ConfigValidationError: If the entry violates the schema.
            SecretConfigError: If the secret reference is invalid (a subclass
                of ConfigValidationError).
        """
        if not isinstance(raw, Mapping):
            raise ConfigValidationError(f"server '{name}': entry must be a mapping")
        provider = ProviderConfig.from_mapping(raw.get("provider"), name)
        client_id = raw.get("client_id")
        if not isinstance(client_id, str) or not client_id.strip():
            raise ConfigValidationError(f"server '{name}': 'client_id' must be a non-empty string")
        secret_env, secret_file = cls._validate_secret_reference(raw, name)
        scopes = cls._validate_scopes(raw.get("scopes"), name)
        button = ButtonConfig.from_mapping(raw.get("button"), name)
        auto_refresh = raw.get("auto_refresh", DEFAULT_AUTO_REFRESH)
        if not isinstance(auto_refresh, bool):
            raise ConfigValidationError(f"server '{name}': 'auto_refresh' must be a boolean")
        return cls(
            name=name,
            provider=provider,
            client_id=client_id.strip(),
            client_secret_env=secret_env,
            client_secret_file=secret_file,
            scopes=scopes,
            button=button,
            auto_refresh=auto_refresh,
        )

    @staticmethod
    def _validate_secret_reference(
        raw: Mapping[str, Any], name: str
    ) -> tuple[str | None, str | None]:
        """Validate that exactly one secret reference is set, never in cleartext.

        Args:
            raw: The raw server mapping.
            name: The server name, for error messages.

        Returns:
            The (client_secret_env, client_secret_file) pair, with the unused
            one set to None.

        Raises:
            SecretConfigError: If a cleartext secret is present, or if the
                references are not exactly one of env or file.
        """
        if "client_secret" in raw:
            log.warning("[SECURITY] server '%s': cleartext 'client_secret' rejected", name)
            raise SecretConfigError(
                f"server '{name}': cleartext 'client_secret' is forbidden; "
                "reference the secret via 'client_secret_env' or 'client_secret_file'"
            )
        env = raw.get("client_secret_env")
        file = raw.get("client_secret_file")
        has_env = env is not None
        has_file = file is not None
        if has_env == has_file:
            raise SecretConfigError(
                f"server '{name}': set exactly one of 'client_secret_env' or 'client_secret_file'"
            )
        if has_env and (not isinstance(env, str) or not env.strip()):
            raise SecretConfigError(
                f"server '{name}': 'client_secret_env' must be a non-empty string"
            )
        if has_file and (not isinstance(file, str) or not file.strip()):
            raise SecretConfigError(
                f"server '{name}': 'client_secret_file' must be a non-empty string"
            )
        return (
            env.strip() if isinstance(env, str) else None,
            file.strip() if isinstance(file, str) else None,
        )

    @staticmethod
    def _validate_scopes(raw: object, name: str) -> tuple[str, ...]:
        """Validate that scopes is a non-empty list of non-empty strings.

        Args:
            raw: The raw scopes value.
            name: The server name, for error messages.

        Returns:
            The validated scopes as a tuple.

        Raises:
            ConfigValidationError: If scopes is missing, empty, or ill-typed.
        """
        if not isinstance(raw, list) or not raw:
            raise ConfigValidationError(
                f"server '{name}': 'scopes' must be a non-empty list of strings"
            )
        scopes: list[str] = []
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                raise ConfigValidationError(
                    f"server '{name}': every scope must be a non-empty string"
                )
            scopes.append(item.strip())
        return tuple(scopes)


@dataclass(frozen=True, slots=True)
class StoreConfig:
    """Encrypted token store: database location and encryption-key source.

    The key is referenced by exactly one of a plain file (recommended at mode
    0400), an environment variable, or a SOPS file. The key value itself is
    never stored in the configuration.
    """

    db_location: str
    key_file: str | None
    key_env: str | None
    key_sops: str | None
    key_sops_key: str | None

    @classmethod
    def from_mapping(cls, raw: object) -> StoreConfig:
        """Validate a raw store mapping into a StoreConfig.

        Args:
            raw: The raw store value from the configuration.

        Returns:
            The validated store configuration.

        Raises:
            ConfigValidationError: If the store section is invalid.
        """
        if not isinstance(raw, Mapping):
            raise ConfigValidationError("'store' must be a mapping")
        db_location = raw.get("db_location")
        if not isinstance(db_location, str) or not db_location.strip():
            raise ConfigValidationError("store: 'db_location' must be a non-empty string")
        key_file = cls._optional_str(raw.get("key_file"), "key_file")
        key_env = cls._optional_str(raw.get("key_env"), "key_env")
        key_sops = cls._optional_str(raw.get("key_sops"), "key_sops")
        present = [
            name
            for name, value in (
                ("key_file", key_file),
                ("key_env", key_env),
                ("key_sops", key_sops),
            )
            if value is not None
        ]
        if len(present) != 1:
            raise ConfigValidationError(
                "store: set exactly one of 'key_file', 'key_env', or 'key_sops'"
            )
        key_sops_key = cls._optional_str(raw.get("key_sops_key"), "key_sops_key")
        if key_sops_key is not None and key_sops is None:
            raise ConfigValidationError(
                "store: 'key_sops_key' is only valid together with 'key_sops'"
            )
        return cls(
            db_location=db_location.strip(),
            key_file=key_file,
            key_env=key_env,
            key_sops=key_sops,
            key_sops_key=key_sops_key,
        )

    @staticmethod
    def _optional_str(value: object, field: str) -> str | None:
        """Return a stripped non-empty string, or None when the value is absent.

        Args:
            value: The raw value.
            field: The field name, for error messages.

        Returns:
            The stripped string, or None when the value is None.

        Raises:
            ConfigValidationError: If the value is present but not a non-empty string.
        """
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ConfigValidationError(f"store: '{field}' must be a non-empty string")
        return value.strip()


_CALLBACK_PATH = "/services/tessera/callback"


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """Deployment-wide service settings: the public Hub base URL.

    The OAuth callback URL is not configurable on its own: it derives from
    ``public_url`` and the fixed JupyterHub service path, so a redirect URI
    can never be built from a client-supplied value.
    """

    public_url: str

    @property
    def redirect_uri(self) -> str:
        """The fixed OAuth callback URL derived from the public base URL.

        This exact value must be registered as the redirect URI at every
        configured OAuth provider.
        """
        return f"{self.public_url}{_CALLBACK_PATH}"

    @classmethod
    def from_mapping(cls, raw: object) -> ServiceConfig:
        """Validate a raw service mapping into a ServiceConfig.

        Args:
            raw: The raw service value from the configuration.

        Returns:
            The validated service configuration.

        Raises:
            ConfigValidationError: If the service section is invalid.
        """
        if not isinstance(raw, Mapping):
            raise ConfigValidationError("'service' must be a mapping")
        url = _validate_url(raw.get("public_url"), "public_url", "service")
        parts = urlsplit(url)
        if parts.query or parts.fragment:
            raise ConfigValidationError("service: 'public_url' must not carry a query or fragment")
        return cls(public_url=url.rstrip("/"))


@dataclass(frozen=True, slots=True)
class TesseraConfig:
    """Top-level tessera configuration: servers, token store, and service."""

    servers: Mapping[str, ServerConfig]
    store: StoreConfig
    service: ServiceConfig

    def get(self, name: str) -> ServerConfig:
        """Return the server configuration for ``name``.

        Args:
            name: The declared server name.

        Returns:
            The matching server configuration.

        Raises:
            KeyError: If no server with that name is declared.
        """
        return self.servers[name]

    @property
    def server_names(self) -> tuple[str, ...]:
        """The declared server names, in declaration order."""
        return tuple(self.servers)
