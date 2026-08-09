"""Tests for the TokenRecord value object."""

from __future__ import annotations

import dataclasses

import pytest

from tessera.store import TokenRecord


def _record(
    *,
    username: str = "alice",
    provider: str = "keycloak",
    refresh_token: str = "rt-secret-value",
    expires_at: float | None = 1000.0,
    scope: str | None = "openid profile",
    refresh_expires_at: float | None = 2000.0,
    created_at: float = 10.0,
    updated_at: float = 20.0,
) -> TokenRecord:
    """Return a valid TokenRecord, with optional field overrides."""
    return TokenRecord(
        username=username,
        provider=provider,
        refresh_token=refresh_token,
        expires_at=expires_at,
        scope=scope,
        refresh_expires_at=refresh_expires_at,
        created_at=created_at,
        updated_at=updated_at,
    )


class TestTokenRecord:
    """TokenRecord is a frozen, slotted, hygiene-aware value object."""

    def test_fields_roundtrip(self) -> None:
        """All fields are stored and read back unchanged."""
        record = _record()
        assert record.username == "alice"
        assert record.provider == "keycloak"
        assert record.refresh_token == "rt-secret-value"
        assert record.expires_at == 1000.0
        assert record.scope == "openid profile"
        assert record.refresh_expires_at == 2000.0
        assert record.created_at == 10.0
        assert record.updated_at == 20.0

    def test_repr_excludes_refresh_token(self) -> None:
        """The refresh token never appears in repr (token hygiene)."""
        record = _record(refresh_token="TOP-SECRET-TOKEN")
        assert "TOP-SECRET-TOKEN" not in repr(record)
        assert "alice" in repr(record)

    def test_is_frozen(self) -> None:
        """A record cannot be mutated after creation."""
        record = _record()
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.username = "bob"  # type: ignore[misc]  # reason: proving frozen

    def test_has_no_dict(self) -> None:
        """Slots keep records lightweight (no per-instance __dict__)."""
        assert not hasattr(_record(), "__dict__")

    def test_optional_fields_accept_none(self) -> None:
        """expires_at, scope, and refresh_expires_at may be None."""
        record = _record(expires_at=None, scope=None, refresh_expires_at=None)
        assert record.expires_at is None
        assert record.scope is None
        assert record.refresh_expires_at is None
