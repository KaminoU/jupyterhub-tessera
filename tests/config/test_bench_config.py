"""The versioned bench configuration is single-sourced and proven loadable.

The Keycloak infrastructure page includes ``infra/servers.yml`` verbatim
via ``literalinclude``, and ``infra/jupyterhub_config.py`` points the
managed service at it; these tests pin that file so the bench
configuration can never drift silently from the documented realms,
clients, and typology axes.
"""

from __future__ import annotations

import json
from pathlib import Path

from tessera.config import load_config

BENCH_PATH = Path(__file__).resolve().parents[2] / "infra" / "servers.yml"
REALM_DIR = Path(__file__).resolve().parents[2] / "infra" / "keycloak"

_STABLE_REALM = "http://localhost:8008/realms/tessera-stable"
_ROTATING_REALM = "http://localhost:8008/realms/tessera-rotating"


def test_bench_file_exists() -> None:
    """The file the bench and the docs point at exists at infra/servers.yml."""
    assert BENCH_PATH.is_file()


def test_bench_config_loads_and_covers_the_typologies() -> None:
    """The bench config loads and pins the documented typology axes."""
    config = load_config(BENCH_PATH)
    assert config.server_names == ("stable-discovery", "stable-explicit", "rotating")

    discovery = config.get("stable-discovery")
    assert discovery.provider.uses_discovery is True
    assert discovery.provider.issuer == _STABLE_REALM
    assert discovery.client_id == "tessera-pkce-required"
    assert "offline_access" in discovery.scopes

    explicit = config.get("stable-explicit")
    assert explicit.provider.uses_discovery is False
    assert explicit.provider.authorization_endpoint == (
        f"{_STABLE_REALM}/protocol/openid-connect/auth"
    )
    assert explicit.provider.token_endpoint == f"{_STABLE_REALM}/protocol/openid-connect/token"
    assert explicit.client_id == "tessera-pkce-optional"
    assert "offline_access" not in explicit.scopes

    rotating = config.get("rotating")
    assert rotating.provider.uses_discovery is True
    assert rotating.provider.issuer == _ROTATING_REALM
    assert rotating.client_id == "tessera-pkce-required"
    assert "offline_access" in rotating.scopes

    for server in (discovery, explicit, rotating):
        assert server.client_secret_env == "TESSERA_CLIENT_SECRET"
        assert server.client_secret_file is None


def test_bench_store_and_service_match_the_bench_contract() -> None:
    """Relative gitignored db, env-held key, and the realm-registered callback."""
    config = load_config(BENCH_PATH)
    assert config.store.db_location == "./tessera-bench.db"
    assert config.store.key_env == "TESSERA_DB_KEY"
    assert config.store.key_file is None
    assert config.service.public_url == "http://localhost:8000"
    # This exact URI is registered on both Keycloak clients (no wildcard).
    assert config.service.redirect_uri == "http://localhost:8000/services/tessera/callback"


def test_realm_users_carry_the_default_realm_roles() -> None:
    """Every imported bench user declares the realm's default composite.

    Declaratively imported users do not receive ``default-roles-<realm>``
    automatically, and that composite carries ``offline_access``: without
    it, Keycloak refuses to deliver offline tokens and the code exchange
    fails with ``not_allowed``.
    """
    for file_name in ("tessera-stable.json", "tessera-rotating.json"):
        realm_export = json.loads((REALM_DIR / file_name).read_text(encoding="utf-8"))
        realm = realm_export["realm"]
        users = realm_export["users"]
        assert len(users) == 3, file_name
        for user in users:
            assert user.get("realmRoles") == [f"default-roles-{realm}"], (
                f"{file_name}: user '{user.get('username')}'"
            )
