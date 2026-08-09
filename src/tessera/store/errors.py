"""Typed errors for the tessera encrypted token store.

The hierarchy is tessera-owned and independent from kstlib's exception
tree: the store catches kstlib's database and encryption errors and
re-raises them as :class:`StoreError` subclasses, so consumers depend only
on tessera's public surface. No error message ever includes a token, a key,
or any other secret value (token hygiene).
"""

from __future__ import annotations


class StoreError(Exception):
    """Base class for every tessera token store error."""


class StoreKeyError(StoreError):
    """The database encryption key could not be acquired.

    Raised when the configured key source cannot be resolved: a key file
    that is missing, unreadable, empty, or too permissive; an environment
    variable that is not set; or a SOPS file that fails to resolve. The key
    value itself is never included in the message.
    """


class StoreDecryptionError(StoreError):
    """The database exists but cannot be opened with the configured key.

    Raised when a key was acquired but SQLCipher rejects it, which means a
    wrong key or a corrupted database. The key value is never included in
    the message.
    """
