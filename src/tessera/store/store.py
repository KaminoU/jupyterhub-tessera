"""Encrypted multi-user token store backed by a SQLCipher database.

The store persists one OAuth refresh token per (username, provider) pair in
a single SQLCipher database that is encrypted at rest as a whole. The
encryption key is resolved from the store configuration (a key file, an
environment variable, or a SOPS file) and is held only by the running
service, never inside the database. The store orchestrates
``kstlib.db.AsyncDatabase``; consumers depend only on tessera's public
surface and its typed errors.

Security invariants:
    - The database is always opened with SQLCipher encryption; an
      unencrypted fallback is never used.
    - A refresh token is passed only as a bound query parameter, never
      interpolated into SQL and never logged.
    - Failing to acquire the key raises :class:`StoreKeyError`; a key that
      does not open the database raises :class:`StoreDecryptionError`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from kstlib.db import AsyncDatabase, DatabaseError

from tessera._shared.secretfile import SecretFileError, read_owner_only_secret
from tessera.config.models import StoreConfig
from tessera.store.errors import StoreDecryptionError, StoreKeyError
from tessera.store.models import TokenRecord

if TYPE_CHECKING:
    from types import TracebackType

log = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tokens (
    username TEXT NOT NULL,
    provider TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at REAL,
    scope TEXT,
    refresh_expires_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (username, provider)
)
"""

_UPSERT_SQL = """
INSERT INTO tokens (username, provider, refresh_token, expires_at, scope,
                    refresh_expires_at, created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (username, provider) DO UPDATE SET
    refresh_token = excluded.refresh_token,
    expires_at = excluded.expires_at,
    scope = excluded.scope,
    refresh_expires_at = excluded.refresh_expires_at,
    updated_at = excluded.updated_at
"""

_SELECT_SQL = """
SELECT username, provider, refresh_token, expires_at, scope,
       refresh_expires_at, created_at, updated_at
FROM tokens
WHERE username = ? AND provider = ?
"""

_DELETE_SQL = "DELETE FROM tokens WHERE username = ? AND provider = ?"
_EXISTS_SQL = "SELECT 1 FROM tokens WHERE username = ? AND provider = ? LIMIT 1"


def _record_from_row(row: tuple[Any, ...]) -> TokenRecord:
    """Build a TokenRecord from a full tokens-table row.

    Args:
        row: A row tuple in the column order of :data:`_SELECT_SQL`.

    Returns:
        The corresponding token record.
    """
    return TokenRecord(
        username=row[0],
        provider=row[1],
        refresh_token=row[2],
        expires_at=row[3],
        scope=row[4],
        refresh_expires_at=row[5],
        created_at=row[6],
        updated_at=row[7],
    )


class TokenStore:
    """Encrypted, multi-user store for OAuth refresh tokens.

    One refresh token is kept per (username, provider) pair in a SQLCipher
    database encrypted at rest. Construct the store from a validated
    :class:`~tessera.config.models.StoreConfig`, then call
    :meth:`initialize` (or use it as an async context manager) before any
    read or write. Driver log hygiene is kstlib's: the pool caps the
    SQLite driver logger so bound parameters (token values) never reach a
    log, even at DEBUG.
    """

    def __init__(
        self,
        config: StoreConfig,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Build a token store from its configuration.

        The encryption key source is resolved eagerly: a missing, empty, or
        (on POSIX) group/world-accessible key file, an unset environment
        variable, or a failing SOPS resolution raises :class:`StoreKeyError`
        here, before any database access.

        Args:
            config: The validated store configuration.
            clock: Callable returning the current Unix time in seconds.
                Injected so tests can control timestamps without sleeping.

        Raises:
            StoreKeyError: If the configured key source cannot be acquired.
        """
        self._clock = clock
        self._db = self._build_database(config)

    def _build_database(self, config: StoreConfig) -> AsyncDatabase:
        """Resolve the key source and build the encrypted database handle.

        Args:
            config: The validated store configuration.

        Returns:
            An AsyncDatabase configured with the resolved cipher key.

        Raises:
            StoreKeyError: If the key source cannot be acquired.
        """
        if config.key_file is not None:
            cipher_key = self._read_key_file(config.key_file)
            return AsyncDatabase(config.db_location, cipher_key=cipher_key)
        try:
            if config.key_env is not None:
                return AsyncDatabase(config.db_location, cipher_env=config.key_env)
            if config.key_sops_key is not None:
                return AsyncDatabase(
                    config.db_location,
                    cipher_sops=config.key_sops,
                    cipher_sops_key=config.key_sops_key,
                )
            return AsyncDatabase(config.db_location, cipher_sops=config.key_sops)
        except DatabaseError as exc:
            raise StoreKeyError("cannot resolve the store encryption key") from exc

    @staticmethod
    def _read_key_file(key_file: str) -> str:
        """Read the SQLCipher key file through the shared owner-only reader.

        Args:
            key_file: Path to the file holding the SQLCipher key.

        Returns:
            The stripped key contents.

        Raises:
            StoreKeyError: If the file is missing, unreadable, empty, holds a
                null byte, or (on POSIX) is accessible by group or others.
        """
        try:
            return read_owner_only_secret(key_file)
        except SecretFileError as exc:
            raise StoreKeyError(str(exc)) from exc

    async def initialize(self) -> None:
        """Open the database and ensure the token table exists.

        The SQLCipher key is validated here: opening a database that was
        written with a different key raises :class:`StoreDecryptionError`.

        Raises:
            StoreDecryptionError: If the database cannot be opened with the
                configured key (wrong key or corrupted database).
        """
        try:
            await self._db.connect()
        except DatabaseError as exc:
            raise StoreDecryptionError(
                "cannot open the token store (wrong key or corrupted database)"
            ) from exc
        await self._db.execute(_CREATE_TABLE_SQL)

    async def save(
        self,
        username: str,
        provider: str,
        refresh_token: str,
        *,
        expires_at: float | None = None,
        scope: str | None = None,
        refresh_expires_at: float | None = None,
    ) -> TokenRecord:
        """Store (or replace) the refresh token for a (username, provider) pair.

        The write is a single atomic upsert: an existing record keeps its
        original ``created_at`` while its token fields and ``updated_at`` are
        refreshed. The returned record reflects the persisted row.

        Args:
            username: The JupyterHub user the token belongs to.
            provider: The OAuth server name the token was issued by.
            refresh_token: The OAuth refresh token to persist.
            expires_at: Unix timestamp when the access token expires, if known.
            scope: The granted scope string, if any.
            refresh_expires_at: Unix timestamp when the refresh token itself
                expires, if the provider dated it.

        Returns:
            The stored token record.
        """
        now = self._clock()
        async with self._db.transaction() as conn:
            await conn.execute(
                _UPSERT_SQL,
                (
                    username,
                    provider,
                    refresh_token,
                    expires_at,
                    scope,
                    refresh_expires_at,
                    now,
                    now,
                ),
            )
        log.debug("stored token for user=%s provider=%s", username, provider)
        row = await self._db.fetch_one(_SELECT_SQL, (username, provider))
        # The row was just written in this committed transaction, so it exists.
        return _record_from_row(cast("tuple[Any, ...]", row))

    async def load(self, username: str, provider: str) -> TokenRecord | None:
        """Return the stored token record for a pair, or None if absent.

        Args:
            username: The JupyterHub user the token belongs to.
            provider: The OAuth server name the token was issued by.

        Returns:
            The stored record, or None when no token exists for the pair.
        """
        row = await self._db.fetch_one(_SELECT_SQL, (username, provider))
        if row is None:
            return None
        return _record_from_row(row)

    async def delete(self, username: str, provider: str) -> bool:
        """Delete the stored token for a pair.

        Args:
            username: The JupyterHub user the token belongs to.
            provider: The OAuth server name the token was issued by.

        Returns:
            True if a record was removed, False if none existed.
        """
        async with self._db.transaction() as conn:
            cursor = await conn.execute(_DELETE_SQL, (username, provider))
            return cursor.rowcount > 0

    async def exists(self, username: str, provider: str) -> bool:
        """Return whether a token is stored for the given pair.

        Args:
            username: The JupyterHub user the token belongs to.
            provider: The OAuth server name the token was issued by.

        Returns:
            True if a token exists for the pair.
        """
        row = await self._db.fetch_one(_EXISTS_SQL, (username, provider))
        return row is not None

    async def close(self) -> None:
        """Close the database and scrub the in-memory key."""
        await self._db.close()

    async def __aenter__(self) -> TokenStore:
        """Initialize the store and return it."""
        await self.initialize()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close the store on context exit."""
        await self.close()
