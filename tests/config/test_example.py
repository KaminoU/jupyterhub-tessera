"""The documented configuration example is single-sourced and proven loadable.

The guide at ``docs/source/guide/configuration.md`` includes
``examples/servers.yml`` verbatim via ``literalinclude``; these tests pin
that same file so the documented example can never drift from a valid
configuration.
"""

from __future__ import annotations

from pathlib import Path

from tessera.config import load_config

EXAMPLE_PATH = Path(__file__).resolve().parents[2] / "examples" / "servers.yml"


def test_example_file_exists() -> None:
    """The example file the guide includes exists at examples/servers.yml."""
    assert EXAMPLE_PATH.is_file()


def test_documented_example_loads_and_validates() -> None:
    """The documented example loads into a valid two-server configuration."""
    config = load_config(EXAMPLE_PATH)
    assert config.server_names == ("demo-oidc", "demo-explicit")

    oidc = config.get("demo-oidc")
    assert oidc.provider.uses_discovery is True
    assert oidc.provider.issuer == "https://idp.example.com/realms/demo"
    assert oidc.client_secret_env == "TESSERA_DEMO_OIDC_SECRET"
    assert oidc.client_secret_file is None
    assert oidc.scopes == ("openid", "profile", "offline_access")

    explicit = config.get("demo-explicit")
    assert explicit.provider.uses_discovery is False
    assert explicit.provider.authorization_endpoint == "https://auth.example.org/oauth2/authorize"
    assert explicit.provider.token_endpoint == "https://auth.example.org/oauth2/token"
    assert explicit.client_secret_file == "/etc/tessera/secrets/demo-explicit.secret"
    assert explicit.client_secret_env is None
    assert explicit.auto_refresh is False

    assert config.store.db_location == "/var/lib/tessera/tokens.db"
    assert config.store.key_file == "/etc/tessera/keys/db.key"
    assert config.store.key_env is None
    assert config.store.key_sops is None

    assert config.service.public_url == "https://hub.example.com"
    assert config.service.redirect_uri == "https://hub.example.com/services/tessera/callback"
