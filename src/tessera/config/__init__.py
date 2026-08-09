"""tessera server configuration: schema, loading, and validation.

Public entry point: :func:`load_config` reads the per-server OAuth
configuration from a YAML file (via ``kstlib.config``) and returns a
validated :class:`TesseraConfig`. All parsing failures surface as
:class:`ConfigError` subclasses.
"""

from __future__ import annotations

from tessera.config.errors import (
    ConfigError,
    ConfigFileError,
    ConfigValidationError,
    SecretConfigError,
)
from tessera.config.loader import (
    CONFIG_ENV_VAR,
    DEFAULT_CONFIG_PATH,
    load_config,
    resolve_config_path,
)
from tessera.config.models import (
    ButtonConfig,
    ProviderConfig,
    ServerConfig,
    ServiceConfig,
    StoreConfig,
    TesseraConfig,
)

__all__ = [
    "CONFIG_ENV_VAR",
    "DEFAULT_CONFIG_PATH",
    "ButtonConfig",
    "ConfigError",
    "ConfigFileError",
    "ConfigValidationError",
    "ProviderConfig",
    "SecretConfigError",
    "ServerConfig",
    "ServiceConfig",
    "StoreConfig",
    "TesseraConfig",
    "load_config",
    "resolve_config_path",
]
