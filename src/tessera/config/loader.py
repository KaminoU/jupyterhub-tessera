"""Load and validate the tessera server configuration.

The loader resolves the configuration file location with a small cascade
(an explicit path, then the ``TESSERA_CONFIG`` environment variable, then
the default user location) and delegates the actual file reading to
``kstlib.config.load_from_file``, which provides safe YAML parsing, a
file-size limit, and optional SOPS decryption. The parsed mapping is then
validated into typed :class:`~tessera.config.models.TesseraConfig` objects.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

import yaml
from kstlib.config import (
    ConfigError as KstlibConfigError,
)
from kstlib.config import (
    ConfigFileNotFoundError,
    load_from_file,
)

from tessera.config.errors import ConfigFileError, ConfigValidationError
from tessera.config.models import ServerConfig, ServiceConfig, StoreConfig, TesseraConfig

log = logging.getLogger(__name__)

CONFIG_ENV_VAR = "TESSERA_CONFIG"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "tessera" / "servers.yml"


def resolve_config_path(path: str | Path | None = None) -> Path:
    """Resolve the configuration file path using the tessera cascade.

    Precedence, highest first: the explicit ``path`` argument, then the
    ``TESSERA_CONFIG`` environment variable, then the default user location
    ``~/.config/tessera/servers.yml``.

    Args:
        path: An explicit path overriding every other source.

    Returns:
        The resolved path (which may or may not exist on disk).
    """
    if path is not None:
        return Path(path)
    env_value = os.environ.get(CONFIG_ENV_VAR)
    if env_value:
        return Path(env_value)
    return DEFAULT_CONFIG_PATH


def load_config(path: str | Path | None = None) -> TesseraConfig:
    """Load, parse, and validate the tessera server configuration.

    Args:
        path: An explicit configuration file path. When omitted, the
            ``TESSERA_CONFIG`` environment variable and then the default
            user location are used.

    Returns:
        The validated top-level configuration.

    Raises:
        ConfigFileError: If the file is missing, too large, or not parseable.
        ConfigValidationError: If the content violates the server schema
            (SecretConfigError, a subclass, for secret-reference issues).
    """
    resolved = resolve_config_path(path)
    try:
        box = load_from_file(resolved)
    except ConfigFileNotFoundError as exc:
        raise ConfigFileError(f"configuration file not found: {resolved}") from exc
    except (KstlibConfigError, yaml.YAMLError) as exc:
        raise ConfigFileError(f"configuration file could not be parsed: {resolved}") from exc
    return _validate_config(box.to_dict())


def _validate_config(data: Mapping[str, object]) -> TesseraConfig:
    """Validate the top-level mapping into a TesseraConfig.

    Args:
        data: The parsed configuration mapping.

    Returns:
        The validated top-level configuration.

    Raises:
        ConfigValidationError: If the ``servers`` mapping is missing, empty,
            or malformed.
    """
    servers_raw = data.get("servers")
    if not isinstance(servers_raw, Mapping) or not servers_raw:
        raise ConfigValidationError("configuration must define a non-empty 'servers' mapping")
    servers: dict[str, ServerConfig] = {}
    for name, raw in servers_raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigValidationError("server names must be non-empty strings")
        servers[name] = ServerConfig.from_mapping(name, raw)
    store_raw = data.get("store")
    if store_raw is None:
        raise ConfigValidationError("configuration must define a 'store' section")
    store = StoreConfig.from_mapping(store_raw)
    service_raw = data.get("service")
    if service_raw is None:
        raise ConfigValidationError("configuration must define a 'service' section")
    service = ServiceConfig.from_mapping(service_raw)
    log.info("Loaded tessera configuration: %d server(s)", len(servers))
    return TesseraConfig(servers=servers, store=store, service=service)
