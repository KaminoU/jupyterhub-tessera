"""Shared owner-only reader for small secret files.

Both the token store (SQLCipher key file) and the OAuth flow (client
secret file) read a secret from a local file. This module owns the single
implementation: on POSIX the file must not be accessible by group or
others, and empty or null-byte contents are refused. The secret value is
returned to the caller and never appears in a message or a log. Callers
wrap :class:`SecretFileError` into their own public error type.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

log = logging.getLogger(__name__)


class SecretFileError(Exception):
    """A secret file could not be read safely.

    Raised when the file is missing, unreadable, empty, holds a null byte,
    or (on POSIX) is accessible by group or others. The secret value is
    never included in the message.
    """


def _key_file_permissions(path: Path) -> int | None:
    """Return the secret file's POSIX permission bits, or None off POSIX.

    On non-POSIX platforms (for example Windows) file permission bits are
    not meaningful for the owner-only check, so None is returned and the
    caller skips the enforcement.

    Args:
        path: Path to the secret file.

    Returns:
        The permission bits (for example 0o600), or None on non-POSIX.

    Raises:
        OSError: If the file cannot be stat-ed on POSIX.
    """
    if os.name != "posix":
        return None
    return stat.S_IMODE(path.stat().st_mode)


def read_owner_only_secret(secret_file: str) -> str:
    """Read and validate a secret file, enforcing tight POSIX permissions.

    Args:
        secret_file: Path to the file holding the secret.

    Returns:
        The stripped secret contents.

    Raises:
        SecretFileError: If the file is missing, unreadable, empty, holds a
            null byte, or (on POSIX) is accessible by group or others.
    """
    path = Path(secret_file)
    try:
        mode = _key_file_permissions(path)
    except OSError as exc:
        raise SecretFileError(f"cannot stat secret file '{secret_file}'") from exc
    if mode is not None and mode & 0o077:
        log.warning(
            "[SECURITY] secret file '%s' is accessible by group or others, rejected",
            secret_file,
        )
        raise SecretFileError(
            f"secret file '{secret_file}' must not be accessible by group or others"
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SecretFileError(f"cannot read secret file '{secret_file}'") from exc
    secret = raw.strip()
    if not secret:
        raise SecretFileError(f"secret file '{secret_file}' is empty")
    if "\x00" in secret:
        raise SecretFileError(f"secret file '{secret_file}' contains a null byte")
    return secret
