"""Tests for the tessera server configuration models and their validation."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from tessera.config.errors import ConfigValidationError, SecretConfigError
from tessera.config.models import (
    ButtonConfig,
    ProviderConfig,
    ServerConfig,
    ServiceConfig,
    StoreConfig,
    TesseraConfig,
)


def _valid_server() -> dict[str, Any]:
    """Return a fresh, fully valid raw server mapping (discovery mode)."""
    return {
        "provider": {"issuer": "https://idp.example.com/realms/demo"},
        "client_id": "tessera-demo-client",
        "client_secret_env": "TESSERA_DEMO_SECRET",
        "scopes": ["openid", "profile"],
        "button": {
            "label": "Demo IDP",
            "color_valid": "#2e7d32",
            "color_invalid": "#c62828",
        },
        "auto_refresh": True,
    }


def _store() -> StoreConfig:
    """Return a valid StoreConfig for TesseraConfig construction tests."""
    return StoreConfig.from_mapping(
        {
            "db_location": "/var/lib/tessera/tokens.db",
            "key_file": "/etc/tessera/keys/db.key",
        }
    )


def _service() -> ServiceConfig:
    """Return a valid ServiceConfig for TesseraConfig construction tests."""
    return ServiceConfig.from_mapping({"public_url": "https://hub.example.com"})


class TestProviderConfig:
    """Validation of the provider block (discovery issuer vs explicit endpoints)."""

    def test_discovery_issuer_valid(self) -> None:
        """A lone issuer selects discovery mode and clears the endpoints."""
        provider = ProviderConfig.from_mapping({"issuer": "https://idp.example.com"}, "s")
        assert provider.uses_discovery is True
        assert provider.issuer == "https://idp.example.com"
        assert provider.authorization_endpoint is None
        assert provider.token_endpoint is None

    def test_explicit_endpoints_valid(self) -> None:
        """Explicit endpoints select non-discovery mode and clear the issuer."""
        provider = ProviderConfig.from_mapping(
            {
                "authorization_endpoint": "https://auth.example.org/authorize",
                "token_endpoint": "https://auth.example.org/token",
            },
            "s",
        )
        assert provider.uses_discovery is False
        assert provider.issuer is None
        assert provider.authorization_endpoint == "https://auth.example.org/authorize"
        assert provider.token_endpoint == "https://auth.example.org/token"

    def test_http_loopback_issuer_allowed(self) -> None:
        """http is accepted for a loopback host (local dev provider)."""
        provider = ProviderConfig.from_mapping({"issuer": "http://localhost:8080/realms/x"}, "s")
        assert provider.issuer == "http://localhost:8080/realms/x"

    def test_http_ipv4_loopback_endpoints_allowed(self) -> None:
        """http is accepted for the 127.0.0.1 loopback address."""
        provider = ProviderConfig.from_mapping(
            {
                "authorization_endpoint": "http://127.0.0.1:9000/authorize",
                "token_endpoint": "http://127.0.0.1:9000/token",
            },
            "s",
        )
        assert provider.token_endpoint == "http://127.0.0.1:9000/token"

    def test_neither_issuer_nor_endpoints_rejected(self) -> None:
        """An empty provider block is rejected."""
        with pytest.raises(ConfigValidationError):
            ProviderConfig.from_mapping({}, "s")

    def test_both_issuer_and_endpoints_rejected(self) -> None:
        """Mixing issuer and explicit endpoints is rejected."""
        with pytest.raises(ConfigValidationError):
            ProviderConfig.from_mapping(
                {
                    "issuer": "https://idp.example.com",
                    "authorization_endpoint": "https://auth.example.org/a",
                },
                "s",
            )

    def test_explicit_missing_token_endpoint_rejected(self) -> None:
        """Explicit mode requires the token endpoint."""
        with pytest.raises(ConfigValidationError):
            ProviderConfig.from_mapping(
                {"authorization_endpoint": "https://auth.example.org/a"}, "s"
            )

    def test_explicit_missing_authorization_endpoint_rejected(self) -> None:
        """Explicit mode requires the authorization endpoint."""
        with pytest.raises(ConfigValidationError):
            ProviderConfig.from_mapping({"token_endpoint": "https://auth.example.org/t"}, "s")

    def test_provider_not_mapping_rejected(self) -> None:
        """A non-mapping provider value is rejected."""
        with pytest.raises(ConfigValidationError):
            ProviderConfig.from_mapping("nope", "s")

    def test_http_non_loopback_rejected(self) -> None:
        """http is rejected for a non-loopback host."""
        with pytest.raises(ConfigValidationError):
            ProviderConfig.from_mapping({"issuer": "http://idp.example.com"}, "s")

    def test_non_http_scheme_rejected(self) -> None:
        """A non-http(s) scheme is rejected."""
        with pytest.raises(ConfigValidationError):
            ProviderConfig.from_mapping({"issuer": "ftp://idp.example.com"}, "s")

    def test_url_without_host_rejected(self) -> None:
        """A URL without a host is rejected."""
        with pytest.raises(ConfigValidationError):
            ProviderConfig.from_mapping({"issuer": "https://"}, "s")

    def test_issuer_non_string_rejected(self) -> None:
        """A non-string issuer is rejected."""
        with pytest.raises(ConfigValidationError):
            ProviderConfig.from_mapping({"issuer": 123}, "s")


class TestButtonConfig:
    """Validation and defaults of the per-server button block."""

    def test_defaults_when_absent(self) -> None:
        """Absent button block yields label=server name and default colors."""
        button = ButtonConfig.from_mapping(None, "myserver")
        assert button.label == "myserver"
        assert button.color_valid == "#2e7d32"
        assert button.color_invalid == "#c62828"

    def test_partial_label_only_keeps_default_colors(self) -> None:
        """A label-only button keeps the default colors."""
        button = ButtonConfig.from_mapping({"label": "Custom"}, "s")
        assert button.label == "Custom"
        assert button.color_valid == "#2e7d32"
        assert button.color_invalid == "#c62828"

    def test_full_custom(self) -> None:
        """A fully specified button keeps every provided value."""
        button = ButtonConfig.from_mapping(
            {"label": "L", "color_valid": "green", "color_invalid": "red"}, "s"
        )
        assert (button.label, button.color_valid, button.color_invalid) == (
            "L",
            "green",
            "red",
        )

    def test_not_mapping_rejected(self) -> None:
        """A non-mapping button value is rejected."""
        with pytest.raises(ConfigValidationError):
            ButtonConfig.from_mapping("nope", "s")

    def test_empty_label_rejected(self) -> None:
        """A blank label is rejected."""
        with pytest.raises(ConfigValidationError):
            ButtonConfig.from_mapping({"label": "   "}, "s")

    def test_non_string_color_rejected(self) -> None:
        """A non-string color is rejected."""
        with pytest.raises(ConfigValidationError):
            ButtonConfig.from_mapping({"color_valid": 123}, "s")


class TestServerConfig:
    """Validation of a full server entry, including secret hygiene."""

    def test_full_valid_env_secret(self) -> None:
        """A complete discovery-mode entry with an env secret validates."""
        server = ServerConfig.from_mapping("demo", _valid_server())
        assert server.name == "demo"
        assert server.client_id == "tessera-demo-client"
        assert server.client_secret_env == "TESSERA_DEMO_SECRET"
        assert server.client_secret_file is None
        assert server.scopes == ("openid", "profile")
        assert server.provider.uses_discovery is True
        assert server.button.label == "Demo IDP"
        assert server.auto_refresh is True

    def test_valid_file_secret(self) -> None:
        """A file-referenced secret validates and clears the env reference."""
        raw = _valid_server()
        del raw["client_secret_env"]
        raw["client_secret_file"] = "/etc/tessera/secrets/demo.secret"
        server = ServerConfig.from_mapping("demo", raw)
        assert server.client_secret_file == "/etc/tessera/secrets/demo.secret"
        assert server.client_secret_env is None

    def test_raw_not_mapping_rejected(self) -> None:
        """A non-mapping server entry is rejected."""
        with pytest.raises(ConfigValidationError):
            ServerConfig.from_mapping("demo", ["nope"])

    def test_missing_client_id_rejected(self) -> None:
        """A missing client_id is rejected."""
        raw = _valid_server()
        del raw["client_id"]
        with pytest.raises(ConfigValidationError):
            ServerConfig.from_mapping("demo", raw)

    def test_empty_client_id_rejected(self) -> None:
        """A blank client_id is rejected."""
        raw = _valid_server()
        raw["client_id"] = "  "
        with pytest.raises(ConfigValidationError):
            ServerConfig.from_mapping("demo", raw)

    def test_cleartext_secret_rejected_and_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """A cleartext client_secret is rejected, logged, and never leaked."""
        raw = _valid_server()
        del raw["client_secret_env"]
        raw["client_secret"] = "super-secret-value"
        with caplog.at_level(logging.WARNING), pytest.raises(SecretConfigError) as excinfo:
            ServerConfig.from_mapping("demo", raw)
        assert "super-secret-value" not in str(excinfo.value)
        assert "[SECURITY]" in caplog.text
        assert "super-secret-value" not in caplog.text

    def test_neither_secret_reference_rejected(self) -> None:
        """Referencing no secret at all is rejected."""
        raw = _valid_server()
        del raw["client_secret_env"]
        with pytest.raises(SecretConfigError):
            ServerConfig.from_mapping("demo", raw)

    def test_both_secret_references_rejected(self) -> None:
        """Referencing both env and file secrets is rejected."""
        raw = _valid_server()
        raw["client_secret_file"] = "/etc/tessera/secrets/demo.secret"
        with pytest.raises(SecretConfigError):
            ServerConfig.from_mapping("demo", raw)

    def test_secret_env_non_string_rejected(self) -> None:
        """A non-string env reference is rejected."""
        raw = _valid_server()
        raw["client_secret_env"] = 123
        with pytest.raises(SecretConfigError):
            ServerConfig.from_mapping("demo", raw)

    def test_secret_file_empty_rejected(self) -> None:
        """A blank file reference is rejected."""
        raw = _valid_server()
        del raw["client_secret_env"]
        raw["client_secret_file"] = "   "
        with pytest.raises(SecretConfigError):
            ServerConfig.from_mapping("demo", raw)

    def test_scopes_missing_rejected(self) -> None:
        """Missing scopes are rejected."""
        raw = _valid_server()
        del raw["scopes"]
        with pytest.raises(ConfigValidationError):
            ServerConfig.from_mapping("demo", raw)

    def test_scopes_empty_list_rejected(self) -> None:
        """An empty scopes list is rejected."""
        raw = _valid_server()
        raw["scopes"] = []
        with pytest.raises(ConfigValidationError):
            ServerConfig.from_mapping("demo", raw)

    def test_scopes_not_a_list_rejected(self) -> None:
        """A scopes value that is not a list is rejected."""
        raw = _valid_server()
        raw["scopes"] = "openid"
        with pytest.raises(ConfigValidationError):
            ServerConfig.from_mapping("demo", raw)

    def test_scopes_non_string_item_rejected(self) -> None:
        """A non-string scope item is rejected."""
        raw = _valid_server()
        raw["scopes"] = ["openid", 123]
        with pytest.raises(ConfigValidationError):
            ServerConfig.from_mapping("demo", raw)

    def test_scopes_empty_string_item_rejected(self) -> None:
        """A blank scope item is rejected."""
        raw = _valid_server()
        raw["scopes"] = ["openid", "  "]
        with pytest.raises(ConfigValidationError):
            ServerConfig.from_mapping("demo", raw)

    def test_auto_refresh_defaults_true(self) -> None:
        """auto_refresh defaults to True when absent."""
        raw = _valid_server()
        del raw["auto_refresh"]
        server = ServerConfig.from_mapping("demo", raw)
        assert server.auto_refresh is True

    def test_auto_refresh_explicit_false(self) -> None:
        """An explicit auto_refresh=false is honored."""
        raw = _valid_server()
        raw["auto_refresh"] = False
        server = ServerConfig.from_mapping("demo", raw)
        assert server.auto_refresh is False

    def test_auto_refresh_non_bool_rejected(self) -> None:
        """A non-boolean auto_refresh is rejected."""
        raw = _valid_server()
        raw["auto_refresh"] = 1
        with pytest.raises(ConfigValidationError):
            ServerConfig.from_mapping("demo", raw)

    def test_button_defaults_when_absent(self) -> None:
        """An absent button block yields defaults derived from the server name."""
        raw = _valid_server()
        del raw["button"]
        server = ServerConfig.from_mapping("demo", raw)
        assert server.button.label == "demo"
        assert server.button.color_valid == "#2e7d32"


class TestTesseraConfig:
    """Behavior of the top-level configuration aggregate."""

    def test_get_returns_server(self) -> None:
        """get returns the requested server configuration."""
        server = ServerConfig.from_mapping("demo", _valid_server())
        config = TesseraConfig(servers={"demo": server}, store=_store(), service=_service())
        assert config.get("demo") is server

    def test_get_missing_raises_keyerror(self) -> None:
        """get raises KeyError for an unknown server."""
        config = TesseraConfig(servers={}, store=_store(), service=_service())
        with pytest.raises(KeyError):
            config.get("nope")

    def test_server_names_preserves_order(self) -> None:
        """server_names lists the declared servers in order."""
        first = ServerConfig.from_mapping("a", _valid_server())
        second = ServerConfig.from_mapping("b", _valid_server())
        config = TesseraConfig(
            servers={"a": first, "b": second}, store=_store(), service=_service()
        )
        assert config.server_names == ("a", "b")


def test_secret_error_is_validation_error() -> None:
    """SecretConfigError is a subclass of ConfigValidationError."""
    assert issubclass(SecretConfigError, ConfigValidationError)
