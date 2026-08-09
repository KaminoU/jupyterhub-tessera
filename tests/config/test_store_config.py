"""Tests for the encrypted token store configuration (StoreConfig)."""

from __future__ import annotations

from typing import Any

import pytest

from tessera.config.errors import ConfigValidationError
from tessera.config.models import StoreConfig


def _valid_store() -> dict[str, Any]:
    """Return a fresh, valid raw store mapping (key by file)."""
    return {
        "db_location": "/var/lib/tessera/tokens.db",
        "key_file": "/etc/tessera/keys/db.key",
    }


class TestStoreConfig:
    """Validation of the top-level store section."""

    def test_valid_key_file(self) -> None:
        """A store keyed by file validates and clears the other sources."""
        store = StoreConfig.from_mapping(_valid_store())
        assert store.db_location == "/var/lib/tessera/tokens.db"
        assert store.key_file == "/etc/tessera/keys/db.key"
        assert store.key_env is None
        assert store.key_sops is None
        assert store.key_sops_key is None

    def test_valid_key_env(self) -> None:
        """A store keyed by environment variable validates."""
        store = StoreConfig.from_mapping(
            {"db_location": "/var/lib/tessera/tokens.db", "key_env": "TESSERA_DB_KEY"}
        )
        assert store.key_env == "TESSERA_DB_KEY"
        assert store.key_file is None

    def test_valid_key_sops_with_sops_key(self) -> None:
        """A store keyed by SOPS accepts an explicit sops key name."""
        store = StoreConfig.from_mapping(
            {
                "db_location": "/var/lib/tessera/tokens.db",
                "key_sops": "/etc/tessera/keys/db.sops.yml",
                "key_sops_key": "database_key",
            }
        )
        assert store.key_sops == "/etc/tessera/keys/db.sops.yml"
        assert store.key_sops_key == "database_key"

    def test_valid_key_sops_without_sops_key(self) -> None:
        """A SOPS-keyed store without an explicit key name leaves it None."""
        store = StoreConfig.from_mapping(
            {
                "db_location": "/var/lib/tessera/tokens.db",
                "key_sops": "/etc/tessera/keys/db.sops.yml",
            }
        )
        assert store.key_sops_key is None

    def test_not_mapping_rejected(self) -> None:
        """A non-mapping store value is rejected."""
        with pytest.raises(ConfigValidationError):
            StoreConfig.from_mapping("nope")

    def test_db_location_missing_rejected(self) -> None:
        """A missing db_location is rejected."""
        with pytest.raises(ConfigValidationError):
            StoreConfig.from_mapping({"key_file": "/etc/tessera/keys/db.key"})

    def test_db_location_empty_rejected(self) -> None:
        """A blank db_location is rejected."""
        with pytest.raises(ConfigValidationError):
            StoreConfig.from_mapping({"db_location": "  ", "key_file": "/etc/tessera/keys/db.key"})

    def test_no_key_source_rejected(self) -> None:
        """A store with no key source is rejected."""
        with pytest.raises(ConfigValidationError):
            StoreConfig.from_mapping({"db_location": "/var/lib/tessera/tokens.db"})

    def test_two_key_sources_rejected(self) -> None:
        """A store referencing two key sources is rejected."""
        with pytest.raises(ConfigValidationError):
            StoreConfig.from_mapping(
                {
                    "db_location": "/var/lib/tessera/tokens.db",
                    "key_file": "/etc/tessera/keys/db.key",
                    "key_env": "TESSERA_DB_KEY",
                }
            )

    def test_three_key_sources_rejected(self) -> None:
        """A store referencing all three key sources is rejected."""
        with pytest.raises(ConfigValidationError):
            StoreConfig.from_mapping(
                {
                    "db_location": "/var/lib/tessera/tokens.db",
                    "key_file": "/etc/tessera/keys/db.key",
                    "key_env": "TESSERA_DB_KEY",
                    "key_sops": "/etc/tessera/keys/db.sops.yml",
                }
            )

    def test_sops_key_without_sops_rejected(self) -> None:
        """key_sops_key without key_sops is rejected."""
        with pytest.raises(ConfigValidationError):
            StoreConfig.from_mapping(
                {
                    "db_location": "/var/lib/tessera/tokens.db",
                    "key_file": "/etc/tessera/keys/db.key",
                    "key_sops_key": "database_key",
                }
            )

    def test_key_file_empty_rejected(self) -> None:
        """A blank key_file is rejected."""
        with pytest.raises(ConfigValidationError):
            StoreConfig.from_mapping(
                {"db_location": "/var/lib/tessera/tokens.db", "key_file": "   "}
            )

    def test_key_env_non_string_rejected(self) -> None:
        """A non-string key_env is rejected."""
        with pytest.raises(ConfigValidationError):
            StoreConfig.from_mapping({"db_location": "/var/lib/tessera/tokens.db", "key_env": 123})
