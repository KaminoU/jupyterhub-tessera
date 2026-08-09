"""Tests for the token store error hierarchy."""

from __future__ import annotations

from tessera.store import StoreDecryptionError, StoreError, StoreKeyError


class TestStoreErrors:
    """The store errors form a tessera-owned hierarchy under StoreError."""

    def test_store_error_is_exception(self) -> None:
        """StoreError is a plain Exception subclass."""
        assert issubclass(StoreError, Exception)

    def test_key_error_is_store_error(self) -> None:
        """StoreKeyError is a StoreError."""
        assert issubclass(StoreKeyError, StoreError)

    def test_decryption_error_is_store_error(self) -> None:
        """StoreDecryptionError is a StoreError."""
        assert issubclass(StoreDecryptionError, StoreError)

    def test_key_and_decryption_are_distinct(self) -> None:
        """Key acquisition and decryption failures are distinct types."""
        assert not issubclass(StoreKeyError, StoreDecryptionError)
        assert not issubclass(StoreDecryptionError, StoreKeyError)
