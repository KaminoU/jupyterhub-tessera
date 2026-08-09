"""Client secret resolution for the OAuth authorization-code flow.

The configuration layer only validates the secret reference (an
environment variable name or a file path, never a cleartext value); this
module performs the first real read of the secret. The resolved value is
returned to the caller and never logged; error messages name the
reference, never the value.
"""

from __future__ import annotations

import os

from tessera._shared.secretfile import SecretFileError, read_owner_only_secret
from tessera.config.models import ServerConfig
from tessera.flow.errors import SecretResolutionError


def resolve_client_secret(server: ServerConfig) -> str:
    """Resolve the client secret referenced by a server configuration.

    The reference is re-validated here instead of trusting the upstream
    configuration checks (defense in depth), and a file-based reference is
    read through the shared owner-only reader, so it obeys the same POSIX
    permission rules as the store key file.

    Args:
        server: The validated server configuration holding the reference.

    Returns:
        The resolved, stripped client secret.

    Raises:
        SecretResolutionError: If no reference is configured, if the
            environment variable is unset or blank, or if the secret file
            is missing, unreadable, empty, holds a null byte, or (on POSIX)
            is accessible by group or others.
    """
    if server.client_secret_env is not None:
        raw = os.environ.get(server.client_secret_env)
        if raw is None:
            raise SecretResolutionError(
                f"server '{server.name}': environment variable "
                f"'{server.client_secret_env}' is not set"
            )
        secret = raw.strip()
        if not secret:
            raise SecretResolutionError(
                f"server '{server.name}': environment variable "
                f"'{server.client_secret_env}' is empty"
            )
        return secret
    if server.client_secret_file is None:
        raise SecretResolutionError(
            f"server '{server.name}': no client secret reference is configured"
        )
    try:
        return read_owner_only_secret(server.client_secret_file)
    except SecretFileError as exc:
        raise SecretResolutionError(f"server '{server.name}': {exc}") from exc
