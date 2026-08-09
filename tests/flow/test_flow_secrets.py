"""Tests for client secret resolution (flow.secrets).

Rejection-first and token-hygiene-heavy: an environment variable or a
secret file that cannot produce a usable value must fail with a typed
error, and the secret value itself must never appear in an error message
or a log record, on success or on failure. The platform-dependent
permission read is patched at its seam in the shared secret-file reader,
never through ``os.name`` (which breaks pathlib on Windows).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import tessera._shared.secretfile as secretfile_module
from tessera.config.models import ButtonConfig, ProviderConfig, ServerConfig
from tessera.flow import FlowError, SecretResolutionError, resolve_client_secret
from tessera.flow.errors import PendingFlowStoreError

_SECRET = "a-very-confidential-client-secret-value"
_ENV_NAME = "TESSERA_TEST_CLIENT_SECRET"


def _server_env(env_name: str = _ENV_NAME) -> ServerConfig:
    """Build a validated server whose secret is referenced by env var."""
    return ServerConfig.from_mapping(
        "demo",
        {
            "provider": {"issuer": "https://idp.example.com/realms/demo"},
            "client_id": "tessera-demo-client",
            "client_secret_env": env_name,
            "scopes": ["openid"],
        },
    )


def _server_file(secret_file: Path) -> ServerConfig:
    """Build a validated server whose secret is referenced by file path."""
    return ServerConfig.from_mapping(
        "demo",
        {
            "provider": {"issuer": "https://idp.example.com/realms/demo"},
            "client_id": "tessera-demo-client",
            "client_secret_file": str(secret_file),
            "scopes": ["openid"],
        },
    )


def _write_secret(tmp_path: Path, content: str = _SECRET) -> Path:
    """Write an owner-only secret file and return its path."""
    path = tmp_path / "client.secret"
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


class TestEnvReference:
    """The environment reference must exist and be non-blank."""

    def test_unset_env_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An environment variable that is not set is refused, named in the message."""
        monkeypatch.delenv(_ENV_NAME, raising=False)
        with pytest.raises(SecretResolutionError) as excinfo:
            resolve_client_secret(_server_env())
        assert _ENV_NAME in str(excinfo.value)

    def test_blank_env_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An environment variable holding only whitespace is refused."""
        monkeypatch.setenv(_ENV_NAME, "   \n")
        with pytest.raises(SecretResolutionError):
            resolve_client_secret(_server_env())

    def test_env_value_resolved_and_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A set environment variable resolves to its stripped value."""
        monkeypatch.setenv(_ENV_NAME, f"  {_SECRET}\n")
        assert resolve_client_secret(_server_env()) == _SECRET


class TestFileReference:
    """The file reference goes through the shared owner-only reader."""

    def test_missing_file_rejected(self, tmp_path: Path) -> None:
        """A secret file that does not exist is refused."""
        with pytest.raises(SecretResolutionError):
            resolve_client_secret(_server_file(tmp_path / "absent.secret"))

    def test_empty_file_rejected(self, tmp_path: Path) -> None:
        """A secret file with only whitespace is refused."""
        with pytest.raises(SecretResolutionError):
            resolve_client_secret(_server_file(_write_secret(tmp_path, content="   \n")))

    def test_null_byte_file_rejected(self, tmp_path: Path) -> None:
        """A secret file containing a null byte is refused."""
        with pytest.raises(SecretResolutionError):
            resolve_client_secret(_server_file(_write_secret(tmp_path, content="abc\x00def")))

    def test_unreadable_file_rejected(self, tmp_path: Path) -> None:
        """A secret path pointing at a directory is refused (unreadable)."""
        secret_dir = tmp_path / "secretdir"
        secret_dir.mkdir()
        secret_dir.chmod(0o700)
        with pytest.raises(SecretResolutionError):
            resolve_client_secret(_server_file(secret_dir))

    def test_group_accessible_file_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A group/world-accessible secret file is refused with a [SECURITY] log."""
        secret_file = _write_secret(tmp_path)
        monkeypatch.setattr(secretfile_module, "_key_file_permissions", lambda path: 0o644)
        with caplog.at_level(logging.DEBUG), pytest.raises(SecretResolutionError) as excinfo:
            resolve_client_secret(_server_file(secret_file))
        assert "[SECURITY]" in caplog.text
        assert _SECRET not in caplog.text
        assert _SECRET not in str(excinfo.value)

    def test_unstattable_file_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A secret file whose permissions cannot be read is refused."""
        secret_file = _write_secret(tmp_path)

        def raise_os(path: Path) -> int | None:
            raise PermissionError("stat denied")

        monkeypatch.setattr(secretfile_module, "_key_file_permissions", raise_os)
        with pytest.raises(SecretResolutionError):
            resolve_client_secret(_server_file(secret_file))

    def test_owner_only_file_resolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An owner-only secret file resolves to its stripped contents."""
        secret_file = _write_secret(tmp_path, content=f"  {_SECRET}\n")
        monkeypatch.setattr(secretfile_module, "_key_file_permissions", lambda path: 0o600)
        assert resolve_client_secret(_server_file(secret_file)) == _SECRET

    def test_non_posix_skips_permission_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A permissive secret file is tolerated when the platform is not POSIX."""
        secret_file = _write_secret(tmp_path)
        secret_file.chmod(0o644)
        monkeypatch.setattr(secretfile_module, "_key_file_permissions", lambda path: None)
        assert resolve_client_secret(_server_file(secret_file)) == _SECRET


class TestDefenseInDepth:
    """A degenerate config that bypassed from_mapping is still refused."""

    def test_reference_free_server_rejected(self) -> None:
        """A server built without any secret reference is refused, not crashed on.

        ``from_mapping`` guarantees exactly one reference, but the dataclass
        constructor does not, so the resolver validates its own input instead
        of trusting upstream (defense in depth).
        """
        server = ServerConfig(
            name="demo",
            provider=ProviderConfig(
                issuer="https://idp.example.com/realms/demo",
                authorization_endpoint=None,
                token_endpoint=None,
            ),
            client_id="tessera-demo-client",
            client_secret_env=None,
            client_secret_file=None,
            scopes=("openid",),
            button=ButtonConfig(label="demo", color_valid="#2e7d32", color_invalid="#c62828"),
            auto_refresh=True,
        )
        with pytest.raises(SecretResolutionError):
            resolve_client_secret(server)


class TestHygieneAndTaxonomy:
    """The secret value never leaks, and the error types sit where expected."""

    def test_secret_never_in_logs_on_success(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Resolving via env and via file emits no log containing the value."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        secret_file = _write_secret(tmp_path)
        monkeypatch.setattr(secretfile_module, "_key_file_permissions", lambda path: 0o600)
        with caplog.at_level(logging.DEBUG):
            assert resolve_client_secret(_server_env()) == _SECRET
            assert resolve_client_secret(_server_file(secret_file)) == _SECRET
        assert _SECRET not in caplog.text

    def test_secret_resolution_error_is_a_flow_error(self) -> None:
        """SecretResolutionError extends FlowError, independent of the store errors."""
        assert issubclass(SecretResolutionError, FlowError)
        assert not issubclass(SecretResolutionError, PendingFlowStoreError)
        assert not issubclass(PendingFlowStoreError, FlowError)
