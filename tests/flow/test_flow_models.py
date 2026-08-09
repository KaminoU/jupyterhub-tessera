"""Tests for the flow value objects (PendingFlow, TokenStatus, ServerInfo)."""

from __future__ import annotations

import dataclasses

import pytest

from tessera.flow import PendingFlow, ServerInfo, TokenStatus


def _flow(
    *,
    username: str = "alice",
    provider: str = "keycloak",
    code_verifier: str = "verifier-value",
    created_at: float = 100.0,
) -> PendingFlow:
    """Return a valid PendingFlow, with optional field overrides."""
    return PendingFlow(
        username=username,
        provider=provider,
        code_verifier=code_verifier,
        created_at=created_at,
    )


class TestPendingFlow:
    """PendingFlow is a frozen, slotted, hygiene-aware value object."""

    def test_fields_roundtrip(self) -> None:
        """All fields are stored and read back unchanged."""
        flow = _flow()
        assert flow.username == "alice"
        assert flow.provider == "keycloak"
        assert flow.code_verifier == "verifier-value"
        assert flow.created_at == 100.0

    def test_repr_excludes_code_verifier(self) -> None:
        """The code verifier never appears in repr (token hygiene)."""
        flow = _flow(code_verifier="TOP-SECRET-VERIFIER")
        assert "TOP-SECRET-VERIFIER" not in repr(flow)
        assert "alice" in repr(flow)

    def test_is_frozen(self) -> None:
        """A flow cannot be mutated after creation."""
        flow = _flow()
        with pytest.raises(dataclasses.FrozenInstanceError):
            flow.username = "bob"  # type: ignore[misc]  # reason: proving frozen

    def test_has_no_dict(self) -> None:
        """Slots keep flows lightweight (no per-instance __dict__)."""
        assert not hasattr(_flow(), "__dict__")


def _status(
    *,
    has_token: bool = True,
    expires_at: float | None = 500.0,
    created_at: float | None = 100.0,
    updated_at: float | None = 200.0,
    scope: str | None = "openid profile offline_access",
    refresh_kind: str = "offline",
    refresh_expires_at: float | None = None,
) -> TokenStatus:
    """Return a valid TokenStatus, with optional field overrides."""
    return TokenStatus(
        has_token=has_token,
        expires_at=expires_at,
        created_at=created_at,
        updated_at=updated_at,
        scope=scope,
        refresh_kind=refresh_kind,
        refresh_expires_at=refresh_expires_at,
    )


class TestTokenStatus:
    """TokenStatus carries the full panel-facing status, frozen and slotted."""

    def test_fields_roundtrip(self) -> None:
        """All fields are stored and read back unchanged."""
        status = _status(refresh_expires_at=900.0)
        assert status.has_token is True
        assert status.expires_at == 500.0
        assert status.created_at == 100.0
        assert status.updated_at == 200.0
        assert status.scope == "openid profile offline_access"
        assert status.refresh_kind == "offline"
        assert status.refresh_expires_at == 900.0

    def test_absent_token_shape(self) -> None:
        """Without a stored token every detail is None and the kind is unknown."""
        status = _status(
            has_token=False,
            expires_at=None,
            created_at=None,
            updated_at=None,
            scope=None,
            refresh_kind="unknown",
            refresh_expires_at=None,
        )
        assert status.has_token is False
        assert status.expires_at is None
        assert status.created_at is None
        assert status.updated_at is None
        assert status.scope is None
        assert status.refresh_kind == "unknown"
        assert status.refresh_expires_at is None

    def test_is_frozen(self) -> None:
        """A status cannot be mutated after creation."""
        status = _status()
        with pytest.raises(dataclasses.FrozenInstanceError):
            status.has_token = False  # type: ignore[misc]  # reason: proving frozen

    def test_has_no_dict(self) -> None:
        """Slots keep statuses lightweight (no per-instance __dict__)."""
        assert not hasattr(_status(), "__dict__")


class TestServerInfo:
    """ServerInfo is the per-server button descriptor served by /servers."""

    def test_fields_roundtrip(self) -> None:
        """All fields are stored and read back unchanged."""
        info = ServerInfo(
            name="stable-discovery",
            label="Stable (discovery)",
            color_valid="#2e7d32",
            color_invalid="#c62828",
        )
        assert info.name == "stable-discovery"
        assert info.label == "Stable (discovery)"
        assert info.color_valid == "#2e7d32"
        assert info.color_invalid == "#c62828"

    def test_is_frozen(self) -> None:
        """A server info cannot be mutated after creation."""
        info = ServerInfo(name="s", label="s", color_valid="#0f0", color_invalid="#f00")
        with pytest.raises(dataclasses.FrozenInstanceError):
            info.name = "other"  # type: ignore[misc]  # reason: proving frozen

    def test_has_no_dict(self) -> None:
        """Slots keep server infos lightweight (no per-instance __dict__)."""
        info = ServerInfo(name="s", label="s", color_valid="#0f0", color_invalid="#f00")
        assert not hasattr(info, "__dict__")
