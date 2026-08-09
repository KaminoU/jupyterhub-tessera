"""Tests for the tessera configuration loader and its path cascade."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tessera.config.errors import ConfigFileError, ConfigValidationError
from tessera.config.loader import (
    CONFIG_ENV_VAR,
    DEFAULT_CONFIG_PATH,
    load_config,
    resolve_config_path,
)
from tessera.config.models import TesseraConfig

_VALID_YAML = textwrap.dedent(
    """\
    service:
      public_url: https://hub.example.com
    store:
      db_location: /var/lib/tessera/tokens.db
      key_file: /etc/tessera/keys/db.key
    servers:
      demo-oidc:
        provider:
          issuer: https://idp.example.com/realms/demo
        client_id: tessera-demo-client
        client_secret_env: TESSERA_DEMO_OIDC_SECRET
        scopes: [openid, profile, offline_access]
        button:
          label: "Demo IDP"
          color_valid: "#2e7d32"
          color_invalid: "#c62828"
        auto_refresh: true
      demo-explicit:
        provider:
          authorization_endpoint: https://auth.example.org/oauth2/authorize
          token_endpoint: https://auth.example.org/oauth2/token
        client_id: tessera-explicit-client
        client_secret_file: /etc/tessera/secrets/demo-explicit.secret
        scopes: [openid]
        auto_refresh: false
    """
)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    """Write ``content`` to ``tmp_path/name`` and return the path."""
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


class TestResolveConfigPath:
    """The kwargs > env > default cascade for the config path."""

    def test_explicit_path_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An explicit path overrides the environment variable."""
        monkeypatch.setenv(CONFIG_ENV_VAR, "/from/env.yml")
        assert resolve_config_path("/explicit.yml") == Path("/explicit.yml")

    def test_env_used_when_no_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The environment variable is used when no explicit path is given."""
        monkeypatch.setenv(CONFIG_ENV_VAR, "/from/env.yml")
        assert resolve_config_path() == Path("/from/env.yml")

    def test_empty_env_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty environment variable falls back to the default path."""
        monkeypatch.setenv(CONFIG_ENV_VAR, "")
        assert resolve_config_path() == DEFAULT_CONFIG_PATH

    def test_default_when_no_path_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The default path is used when neither a path nor env is set."""
        monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
        assert resolve_config_path() == DEFAULT_CONFIG_PATH


class TestLoadConfig:
    """Loading, wrapping of file errors, and top-level validation."""

    def test_valid_two_servers(self, tmp_path: Path) -> None:
        """A valid two-server file loads into a TesseraConfig."""
        path = _write(tmp_path, "servers.yml", _VALID_YAML)
        config = load_config(path)
        assert isinstance(config, TesseraConfig)
        assert config.server_names == ("demo-oidc", "demo-explicit")
        oidc = config.get("demo-oidc")
        assert oidc.provider.uses_discovery is True
        assert oidc.client_secret_env == "TESSERA_DEMO_OIDC_SECRET"
        explicit = config.get("demo-explicit")
        assert explicit.provider.uses_discovery is False
        assert explicit.client_secret_file == "/etc/tessera/secrets/demo-explicit.secret"
        assert explicit.auto_refresh is False
        assert config.store.db_location == "/var/lib/tessera/tokens.db"
        assert config.store.key_file == "/etc/tessera/keys/db.key"
        assert config.store.key_env is None
        assert config.service.public_url == "https://hub.example.com"
        assert config.service.redirect_uri == ("https://hub.example.com/services/tessera/callback")

    def test_load_via_env_var(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """load_config with no argument honors TESSERA_CONFIG."""
        path = _write(tmp_path, "servers.yml", _VALID_YAML)
        monkeypatch.setenv(CONFIG_ENV_VAR, str(path))
        config = load_config()
        assert config.server_names == ("demo-oidc", "demo-explicit")

    def test_missing_file_wrapped(self, tmp_path: Path) -> None:
        """A missing file is wrapped into ConfigFileError."""
        with pytest.raises(ConfigFileError):
            load_config(tmp_path / "does-not-exist.yml")

    def test_malformed_yaml_wrapped(self, tmp_path: Path) -> None:
        """Malformed YAML is wrapped into ConfigFileError."""
        path = _write(tmp_path, "bad.yml", "servers: {unclosed\n")
        with pytest.raises(ConfigFileError):
            load_config(path)

    def test_unsupported_extension_wrapped(self, tmp_path: Path) -> None:
        """An unsupported file extension is wrapped into ConfigFileError."""
        path = _write(tmp_path, "servers.txt", _VALID_YAML)
        with pytest.raises(ConfigFileError):
            load_config(path)

    def test_oversized_file_wrapped(self, tmp_path: Path) -> None:
        """A file above the kstlib size limit is wrapped into ConfigFileError."""
        oversized = "servers:\n  demo: " + "a" * (10 * 1024 * 1024)
        path = _write(tmp_path, "big.yml", oversized)
        with pytest.raises(ConfigFileError):
            load_config(path)

    def test_empty_config_rejected(self, tmp_path: Path) -> None:
        """An empty configuration file is rejected."""
        path = _write(tmp_path, "empty.yml", "\n")
        with pytest.raises(ConfigValidationError):
            load_config(path)

    def test_servers_not_mapping_rejected(self, tmp_path: Path) -> None:
        """A servers value that is not a mapping is rejected."""
        path = _write(tmp_path, "list.yml", "servers:\n  - a\n  - b\n")
        with pytest.raises(ConfigValidationError):
            load_config(path)

    def test_non_string_server_key_rejected(self, tmp_path: Path) -> None:
        """A non-string server key is rejected."""
        path = _write(tmp_path, "intkey.yml", "servers:\n  1:\n    client_id: x\n")
        with pytest.raises(ConfigValidationError):
            load_config(path)

    def test_blank_server_key_rejected(self, tmp_path: Path) -> None:
        """A blank server key is rejected."""
        path = _write(tmp_path, "blankkey.yml", 'servers:\n  "  ":\n    client_id: x\n')
        with pytest.raises(ConfigValidationError):
            load_config(path)

    def test_store_missing_rejected(self, tmp_path: Path) -> None:
        """A configuration with valid servers but no store is rejected."""
        content = (
            "service:\n"
            "  public_url: https://hub.example.com\n"
            "servers:\n"
            "  demo:\n"
            "    provider:\n"
            "      issuer: https://idp.example.com\n"
            "    client_id: tessera-demo-client\n"
            "    client_secret_env: TESSERA_DEMO_SECRET\n"
            "    scopes: [openid]\n"
        )
        path = _write(tmp_path, "nostore.yml", content)
        with pytest.raises(ConfigValidationError):
            load_config(path)

    def test_service_missing_rejected(self, tmp_path: Path) -> None:
        """A configuration with servers and store but no service is rejected."""
        content = (
            "store:\n"
            "  db_location: /var/lib/tessera/tokens.db\n"
            "  key_file: /etc/tessera/keys/db.key\n"
            "servers:\n"
            "  demo:\n"
            "    provider:\n"
            "      issuer: https://idp.example.com\n"
            "    client_id: tessera-demo-client\n"
            "    client_secret_env: TESSERA_DEMO_SECRET\n"
            "    scopes: [openid]\n"
        )
        path = _write(tmp_path, "noservice.yml", content)
        with pytest.raises(ConfigValidationError):
            load_config(path)
