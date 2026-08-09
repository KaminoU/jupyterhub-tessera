"""tessera encrypted token store: the SQLCipher-backed multi-user store.

Public entry points: :class:`TokenStore` (the async store) and
:class:`TokenRecord` (a stored token), plus the typed :class:`StoreError`
hierarchy. The database is encrypted at rest via kstlib's SQLCipher
integration; the encryption key is held by the service, never in the
database.
"""

from __future__ import annotations

from tessera.store.errors import StoreDecryptionError, StoreError, StoreKeyError
from tessera.store.models import TokenRecord
from tessera.store.store import TokenStore

__all__ = [
    "StoreDecryptionError",
    "StoreError",
    "StoreKeyError",
    "TokenRecord",
    "TokenStore",
]
