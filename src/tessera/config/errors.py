"""Typed errors for the tessera server configuration layer.

The hierarchy is tessera-owned and independent from kstlib's exception
tree: the loader catches kstlib's specific configuration errors and
re-raises them as :class:`ConfigError` subclasses, so consumers depend
only on tessera's public surface.
"""

from __future__ import annotations


class ConfigError(Exception):
    """Base class for every tessera configuration error."""


class ConfigFileError(ConfigError):
    """The configuration file is missing, unreadable, too large, or malformed."""


class ConfigValidationError(ConfigError):
    """The configuration content violates the tessera server schema."""


class SecretConfigError(ConfigValidationError):
    """A client secret is referenced incorrectly or exposed in cleartext.

    Raised when a server entry embeds a cleartext ``client_secret`` value,
    or when it does not reference exactly one of ``client_secret_env`` or
    ``client_secret_file``. The offending value is never included in the
    message (token hygiene).
    """
