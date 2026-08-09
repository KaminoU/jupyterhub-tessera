"""Tests for the encrypted multi-user token store (TokenStore).

The suite is security-heavy and rejection-first: it proves that data is
encrypted at rest, that a wrong or missing key is refused with the right
typed error, that records are isolated per (user, provider), and that a
refresh token never leaks into a log. The clock is injected so timestamps
are deterministic without any real sleep.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import tessera._shared.secretfile as secretfile_module
from tessera.config import StoreConfig
from tessera.store import StoreDecryptionError, StoreKeyError, TokenRecord, TokenStore

_KEY = "correct-horse-battery-staple-a-strong-test-passphrase"
_OTHER_KEY = "a-completely-different-passphrase-for-the-wrong-key-case"


def _write_key(tmp_path: Path, content: str = _KEY, name: str = "db.key") -> Path:
    """Write a key file (owner-only) and return its path."""
    key_path = tmp_path / name
    key_path.write_text(content, encoding="utf-8")
    key_path.chmod(0o600)  # owner-only, required by the store on POSIX
    return key_path


def _config(tmp_path: Path, key_path: Path, db_name: str = "tokens.db") -> StoreConfig:
    """Build a file-keyed StoreConfig pointing at a temp db and key."""
    return StoreConfig.from_mapping(
        {"db_location": str(tmp_path / db_name), "key_file": str(key_path)}
    )


class _Clock:
    """A controllable clock returning a fixed value until moved."""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class TestRoundtrip:
    """Saved tokens survive reads and reopening with the same key."""

    async def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        """A saved token is read back with all fields intact."""
        config = _config(tmp_path, _write_key(tmp_path))
        async with TokenStore(config, clock=_Clock(100.0)) as store:
            saved = await store.save(
                "alice",
                "keycloak",
                "rt-1",
                expires_at=500.0,
                scope="openid",
                refresh_expires_at=800.0,
            )
            loaded = await store.load("alice", "keycloak")
        assert loaded == saved
        assert loaded is not None
        assert loaded.refresh_token == "rt-1"
        assert loaded.expires_at == 500.0
        assert loaded.scope == "openid"
        assert loaded.refresh_expires_at == 800.0
        assert loaded.created_at == 100.0
        assert loaded.updated_at == 100.0

    async def test_persists_across_reopen_with_same_key(self, tmp_path: Path) -> None:
        """Data survives closing and reopening with the same key."""
        config = _config(tmp_path, _write_key(tmp_path))
        async with TokenStore(config, clock=_Clock(100.0)) as store:
            await store.save("alice", "keycloak", "rt-persist")
        async with TokenStore(config, clock=_Clock(200.0)) as store2:
            loaded = await store2.load("alice", "keycloak")
        assert loaded is not None
        assert loaded.refresh_token == "rt-persist"

    async def test_save_returns_persisted_record(self, tmp_path: Path) -> None:
        """save returns the record it persisted."""
        config = _config(tmp_path, _write_key(tmp_path))
        async with TokenStore(config, clock=_Clock(100.0)) as store:
            record = await store.save("bob", "okta", "rt-bob")
        assert isinstance(record, TokenRecord)
        assert record.username == "bob"
        assert record.provider == "okta"
        assert record.refresh_token == "rt-bob"
        assert record.expires_at is None
        assert record.scope is None
        assert record.refresh_expires_at is None
        assert record.created_at == 100.0


class TestCrud:
    """Basic load / exists / delete semantics."""

    async def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        """Loading an unknown pair returns None."""
        config = _config(tmp_path, _write_key(tmp_path))
        async with TokenStore(config) as store:
            assert await store.load("nobody", "nowhere") is None

    async def test_exists_reflects_presence(self, tmp_path: Path) -> None:
        """exists is False before a save and True after."""
        config = _config(tmp_path, _write_key(tmp_path))
        async with TokenStore(config) as store:
            assert await store.exists("alice", "keycloak") is False
            await store.save("alice", "keycloak", "rt")
            assert await store.exists("alice", "keycloak") is True

    async def test_delete_present_returns_true(self, tmp_path: Path) -> None:
        """Deleting an existing pair removes it and returns True."""
        config = _config(tmp_path, _write_key(tmp_path))
        async with TokenStore(config) as store:
            await store.save("alice", "keycloak", "rt")
            assert await store.delete("alice", "keycloak") is True
            assert await store.exists("alice", "keycloak") is False

    async def test_delete_absent_returns_false(self, tmp_path: Path) -> None:
        """Deleting an unknown pair returns False."""
        config = _config(tmp_path, _write_key(tmp_path))
        async with TokenStore(config) as store:
            assert await store.delete("ghost", "nowhere") is False

    async def test_explicit_initialize_and_close(self, tmp_path: Path) -> None:
        """The store works without the context manager, closed explicitly."""
        config = _config(tmp_path, _write_key(tmp_path))
        store = TokenStore(config)
        await store.initialize()
        try:
            await store.save("alice", "keycloak", "rt")
            assert await store.exists("alice", "keycloak") is True
        finally:
            await store.close()


class TestIsolation:
    """Records are keyed strictly by (username, provider)."""

    async def test_isolated_between_users(self, tmp_path: Path) -> None:
        """Two users under the same provider keep distinct tokens."""
        config = _config(tmp_path, _write_key(tmp_path))
        async with TokenStore(config) as store:
            await store.save("alice", "keycloak", "rt-alice")
            await store.save("bob", "keycloak", "rt-bob")
            alice = await store.load("alice", "keycloak")
            bob = await store.load("bob", "keycloak")
        assert alice is not None and alice.refresh_token == "rt-alice"
        assert bob is not None and bob.refresh_token == "rt-bob"

    async def test_isolated_between_providers(self, tmp_path: Path) -> None:
        """One user under two providers keeps distinct tokens."""
        config = _config(tmp_path, _write_key(tmp_path))
        async with TokenStore(config) as store:
            await store.save("alice", "keycloak", "rt-kc")
            await store.save("alice", "okta", "rt-okta")
            keycloak = await store.load("alice", "keycloak")
            okta = await store.load("alice", "okta")
        assert keycloak is not None and keycloak.refresh_token == "rt-kc"
        assert okta is not None and okta.refresh_token == "rt-okta"


class TestUpsert:
    """Re-saving a pair updates atomically and preserves created_at."""

    async def test_update_preserves_created_at_and_bumps_updated_at(self, tmp_path: Path) -> None:
        """A second save keeps created_at but refreshes token and updated_at."""
        config = _config(tmp_path, _write_key(tmp_path))
        clock = _Clock(100.0)
        async with TokenStore(config, clock=clock) as store:
            first = await store.save("alice", "keycloak", "rt-old", scope="openid")
            assert first.created_at == 100.0
            assert first.updated_at == 100.0
            clock.now = 250.0
            second = await store.save("alice", "keycloak", "rt-new", scope="openid profile")
            assert second.refresh_token == "rt-new"
            assert second.scope == "openid profile"
            assert second.created_at == 100.0  # preserved
            assert second.updated_at == 250.0  # bumped
            assert await store.exists("alice", "keycloak") is True

    async def test_update_overwrites_refresh_expires_at_including_null(
        self, tmp_path: Path
    ) -> None:
        """An update replaces refresh_expires_at, including back to None."""
        config = _config(tmp_path, _write_key(tmp_path))
        async with TokenStore(config, clock=_Clock(100.0)) as store:
            first = await store.save("alice", "keycloak", "rt-1", refresh_expires_at=400.0)
            assert first.refresh_expires_at == 400.0
            second = await store.save("alice", "keycloak", "rt-1", refresh_expires_at=700.0)
            assert second.refresh_expires_at == 700.0
            third = await store.save("alice", "keycloak", "rt-1")
            assert third.refresh_expires_at is None
            loaded = await store.load("alice", "keycloak")
            assert loaded is not None
            assert loaded.refresh_expires_at is None


class TestKeyAcquisition:
    """A key source that cannot be resolved raises StoreKeyError at construct."""

    async def test_missing_key_file_raises_store_key_error(self, tmp_path: Path) -> None:
        """A key file that does not exist is refused."""
        config = StoreConfig.from_mapping(
            {"db_location": str(tmp_path / "t.db"), "key_file": str(tmp_path / "absent.key")}
        )
        with pytest.raises(StoreKeyError):
            TokenStore(config)

    async def test_empty_key_file_raises_store_key_error(self, tmp_path: Path) -> None:
        """A key file with only whitespace is refused."""
        config = _config(tmp_path, _write_key(tmp_path, content="   \n"))
        with pytest.raises(StoreKeyError):
            TokenStore(config)

    async def test_null_byte_key_file_rejected(self, tmp_path: Path) -> None:
        """A key file containing a null byte is refused."""
        config = _config(tmp_path, _write_key(tmp_path, content="abc\x00def"))
        with pytest.raises(StoreKeyError):
            TokenStore(config)

    async def test_key_file_that_is_a_directory_rejected(self, tmp_path: Path) -> None:
        """A key path pointing at a directory is refused (unreadable)."""
        key_dir = tmp_path / "keydir"
        key_dir.mkdir()
        key_dir.chmod(0o700)
        config = StoreConfig.from_mapping(
            {"db_location": str(tmp_path / "t.db"), "key_file": str(key_dir)}
        )
        with pytest.raises(StoreKeyError):
            TokenStore(config)

    async def test_unset_env_key_raises_store_key_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An environment key source that is not set is refused."""
        monkeypatch.delenv("TESSERA_TEST_DB_KEY", raising=False)
        config = StoreConfig.from_mapping(
            {"db_location": str(tmp_path / "t.db"), "key_env": "TESSERA_TEST_DB_KEY"}
        )
        with pytest.raises(StoreKeyError):
            TokenStore(config)

    async def test_env_key_roundtrip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An environment-sourced key opens and persists the store."""
        monkeypatch.setenv("TESSERA_TEST_DB_KEY", _KEY)
        config = StoreConfig.from_mapping(
            {"db_location": str(tmp_path / "t.db"), "key_env": "TESSERA_TEST_DB_KEY"}
        )
        async with TokenStore(config) as store:
            await store.save("alice", "keycloak", "rt-env")
            loaded = await store.load("alice", "keycloak")
        assert loaded is not None and loaded.refresh_token == "rt-env"

    async def test_sops_source_failure_raises_store_key_error(self, tmp_path: Path) -> None:
        """A SOPS source that cannot be resolved is refused."""
        config = StoreConfig.from_mapping(
            {"db_location": str(tmp_path / "t.db"), "key_sops": str(tmp_path / "absent.sops.yml")}
        )
        with pytest.raises(StoreKeyError):
            TokenStore(config)

    async def test_sops_source_with_named_entry_failure_raises_store_key_error(
        self, tmp_path: Path
    ) -> None:
        """A SOPS source with an explicit entry name still refuses on failure."""
        config = StoreConfig.from_mapping(
            {
                "db_location": str(tmp_path / "t.db"),
                "key_sops": str(tmp_path / "absent.sops.yml"),
                "key_sops_key": "database_key",
            }
        )
        with pytest.raises(StoreKeyError):
            TokenStore(config)


class TestKeyFilePermissions:
    """On POSIX the key file must be owner-only; other platforms skip it.

    The platform-dependent permission read is a private seam
    (``_key_file_permissions``, hosted by the shared secret-file reader in
    ``tessera._shared.secretfile``) so the reject / accept / skip /
    stat-error branches can be exercised deterministically on any host.
    Mocking ``os.name`` globally is avoided on purpose: it breaks
    ``pathlib`` on Windows, where a PosixPath cannot be instantiated.
    """

    async def test_group_readable_key_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A group/world-accessible key file is refused."""
        config = _config(tmp_path, _write_key(tmp_path))
        monkeypatch.setattr(secretfile_module, "_key_file_permissions", lambda path: 0o644)
        with pytest.raises(StoreKeyError):
            TokenStore(config)

    async def test_owner_only_key_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An owner-only key file is accepted."""
        config = _config(tmp_path, _write_key(tmp_path))
        monkeypatch.setattr(secretfile_module, "_key_file_permissions", lambda path: 0o600)
        assert isinstance(TokenStore(config), TokenStore)

    async def test_non_posix_skips_permission_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A permissive key file is tolerated when the platform is not POSIX."""
        key = _write_key(tmp_path)
        key.chmod(0o644)
        config = _config(tmp_path, key)
        monkeypatch.setattr(secretfile_module, "_key_file_permissions", lambda path: None)
        assert isinstance(TokenStore(config), TokenStore)

    async def test_unstattable_key_file_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A key file whose permissions cannot be read is refused."""
        config = _config(tmp_path, _write_key(tmp_path))

        def raise_os(path: Path) -> int | None:
            raise PermissionError("stat denied")

        monkeypatch.setattr(secretfile_module, "_key_file_permissions", raise_os)
        with pytest.raises(StoreKeyError):
            TokenStore(config)

    def test_permissions_helper_reads_mode_on_posix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The helper returns the file mode when the platform is POSIX."""
        key = tmp_path / "probe.key"  # constructed before patching os.name
        key.write_text("secret", encoding="utf-8")
        monkeypatch.setattr(Path, "stat", lambda self, *a, **k: SimpleNamespace(st_mode=0o100600))
        monkeypatch.setattr(os, "name", "posix")
        result = secretfile_module._key_file_permissions(key)
        monkeypatch.undo()  # restore before asserting so a failure formats cleanly
        assert result == 0o600

    def test_permissions_helper_returns_none_off_posix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The helper returns None when the platform is not POSIX."""
        key = tmp_path / "probe.key"
        key.write_text("secret", encoding="utf-8")
        monkeypatch.setattr(os, "name", "nt")
        result = secretfile_module._key_file_permissions(key)
        monkeypatch.undo()
        assert result is None


class TestDecryption:
    """A key that does not open the database raises StoreDecryptionError."""

    async def test_wrong_key_raises_decryption_error(self, tmp_path: Path) -> None:
        """Reopening a db with a different key is refused at initialize."""
        config = _config(tmp_path, _write_key(tmp_path, content=_KEY, name="good.key"))
        async with TokenStore(config, clock=_Clock(100.0)) as store:
            await store.save("alice", "keycloak", "rt-secret")
        wrong_config = StoreConfig.from_mapping(
            {
                "db_location": config.db_location,
                "key_file": str(_write_key(tmp_path, content=_OTHER_KEY, name="wrong.key")),
            }
        )
        store2 = TokenStore(wrong_config)
        try:
            with pytest.raises(StoreDecryptionError):
                await store2.initialize()
        finally:
            await store2.close()


class TestEncryptionAtRest:
    """The database file is genuinely encrypted."""

    async def test_database_file_does_not_contain_plaintext_token(self, tmp_path: Path) -> None:
        """The persisted token is not readable in the raw database bytes."""
        config = _config(tmp_path, _write_key(tmp_path))
        secret = "PLAINTEXT-REFRESH-TOKEN-should-be-encrypted"
        async with TokenStore(config, clock=_Clock(100.0)) as store:
            await store.save("alice", "keycloak", secret)
        raw = Path(config.db_location).read_bytes()
        assert secret.encode("utf-8") not in raw
        # A SQLCipher database does not begin with the plaintext SQLite header.
        assert not raw.startswith(b"SQLite format 3\x00")


class TestTokenHygiene:
    """A refresh token never reaches a log record."""

    async def test_token_never_appears_in_logs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No log entry (even DEBUG) contains the refresh token."""
        config = _config(tmp_path, _write_key(tmp_path))
        secret = "ULTRA-SECRET-REFRESH-TOKEN-xyz"
        with caplog.at_level(logging.DEBUG):
            async with TokenStore(config) as store:
                await store.save("alice", "keycloak", secret)
                await store.load("alice", "keycloak")
        assert secret not in caplog.text


class TestConcurrentSaves:
    """Functional concurrency: parallel writes settle into a coherent state."""

    async def test_parallel_saves_all_land(self, tmp_path: Path) -> None:
        """Concurrent saves on a hot pair and on distinct pairs all commit.

        The hot pair must hold exactly one of the concurrently written
        tokens (atomic upsert, no lost or interleaved write), and every
        distinct pair must hold exactly the token written for it.
        """
        config = _config(tmp_path, _write_key(tmp_path))
        async with TokenStore(config, clock=_Clock(100.0)) as store:
            hot = [store.save("alice", "keycloak", f"rt-hot-{i}") for i in range(5)]
            cold = [store.save(f"user-{i}", "keycloak", f"rt-cold-{i}") for i in range(5)]
            await asyncio.gather(*hot, *cold)
            alice = await store.load("alice", "keycloak")
            assert alice is not None
            assert alice.refresh_token in {f"rt-hot-{i}" for i in range(5)}
            for i in range(5):
                record = await store.load(f"user-{i}", "keycloak")
                assert record is not None
                assert record.refresh_token == f"rt-cold-{i}"
