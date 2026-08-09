"""Run the tessera Service from the JupyterHub-provided environment.

``python -m tessera.service`` starts the tornado application in both
deployment modes: Hub-managed (the Hub spawns the process and injects
the environment) and externally managed (the deployment sets the same
variables). The tessera configuration itself is loaded through the
usual cascade (``TESSERA_CONFIG``, then the default user location).

Environment contract, set by JupyterHub for managed services:
    - ``JUPYTERHUB_SERVICE_URL``: the http URL to bind (host and port).
    - ``JUPYTERHUB_SERVICE_PREFIX``: the public route prefix.
    - ``JUPYTERHUB_API_TOKEN``: the service's Hub API token (read by the
      Hub authenticators; the runner only checks its presence).

Startup validates the deployment before binding: a missing variable, an
unsupported bind scheme, or a redirect URI that does not match the
service mount fails fast with one actionable message, never on the
first request.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from jupyterhub.services.auth import HubAuth, HubOAuth

from tessera.config.errors import ConfigError
from tessera.config.loader import load_config
from tessera.flow.orchestrator import FlowOrchestrator
from tessera.flow.store import PendingFlowStore
from tessera.service.app import make_app
from tessera.store.errors import StoreError
from tessera.store.store import TokenStore

if TYPE_CHECKING:
    from tessera.config.models import TesseraConfig

log = logging.getLogger(__name__)

_EXECUTOR_MAX_WORKERS = 8
_ENV_SERVICE_URL = "JUPYTERHUB_SERVICE_URL"
_ENV_SERVICE_PREFIX = "JUPYTERHUB_SERVICE_PREFIX"
_ENV_API_TOKEN = "JUPYTERHUB_API_TOKEN"
_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

LOG_FORMAT = "[TESSERA ::: %(asctime)s ::: %(levelname)s] %(name)s: %(message)s"
"""Greppable log line format applied to the ``tessera.*`` loggers."""

_QUIET_THIRD_PARTY = ("httpx", "kstlib.config.loader", "kstlib.auth")


def configure_logging(level: str) -> None:
    """Configure the tessera loggers and quiet the noisy third-party ones.

    A dedicated stream handler carrying the greppable :data:`LOG_FORMAT`
    is attached to the ``tessera`` package logger, which stops
    propagating so the tessera format never leaks onto third-party
    records. The known chatty third-party loggers (an HTTP client that
    logs every request, a config loader that logs every load, and the
    auth library) are pinned to WARNING by default: belt-and-suspenders,
    since the root logger stays at WARNING anyway, but explicit about the
    intent and resilient to a deployment that raises the root. A
    deployment may raise them again afterwards, since Python loggers are
    process-global. The function is idempotent: re-running it never stacks
    duplicate handlers.

    Args:
        level: A validated logging level name (see :data:`_LOG_LEVELS`).
    """
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    tessera_logger = logging.getLogger("tessera")
    for existing in list(tessera_logger.handlers):
        tessera_logger.removeHandler(existing)
    tessera_logger.addHandler(handler)
    tessera_logger.setLevel(level)
    tessera_logger.propagate = False
    for name in _QUIET_THIRD_PARTY:
        logging.getLogger(name).setLevel(logging.WARNING)


class ServiceStartupError(Exception):
    """The service environment or configuration prevents startup.

    Raised before the server binds, so a deployment problem fails fast
    with an actionable message instead of surfacing on the first
    request. Messages name variables and paths, never secret values.
    """


def _require_env(name: str) -> str:
    """Return a required environment variable, stripped.

    Args:
        name: The variable name.

    Returns:
        The non-empty, stripped value.

    Raises:
        ServiceStartupError: If the variable is unset or blank.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise ServiceStartupError(
            f"environment variable '{name}' is required (JupyterHub sets it for managed services)"
        )
    return value


def _bind_address(service_url: str) -> tuple[str, int]:
    """Extract the host and port to bind from the service URL.

    Args:
        service_url: The value of ``JUPYTERHUB_SERVICE_URL``.

    Returns:
        The (host, port) pair to listen on.

    Raises:
        ServiceStartupError: If the URL is not an http URL with an
            explicit port (the service never terminates TLS itself).
    """
    parts = urlsplit(service_url)
    if parts.scheme == "https":
        raise ServiceStartupError(
            f"'{_ENV_SERVICE_URL}' uses https: the tessera service does not "
            "terminate TLS itself, bind it on http behind the Hub proxy"
        )
    try:
        port = parts.port
    except ValueError:
        port = None
    if parts.scheme != "http" or not parts.hostname or port is None:
        raise ServiceStartupError(f"'{_ENV_SERVICE_URL}' must be an http URL with an explicit port")
    return parts.hostname, port


def _check_callback_path(config: TesseraConfig, prefix: str) -> None:
    """Fail fast when the redirect URI does not match the service mount.

    The redirect URI is derived from ``service.public_url`` and a fixed
    service path, while the Hub mounts the service at its own prefix; a
    mismatch would break every callback, so it is a startup error, not a
    request-time surprise.

    Args:
        config: The validated tessera configuration.
        prefix: The service prefix the routes are mounted under.

    Raises:
        ServiceStartupError: If the configured callback path is not
            served by this mount.
    """
    expected = "/" + prefix.strip("/") + "/callback"
    actual = urlsplit(config.service.redirect_uri).path
    if actual != expected:
        raise ServiceStartupError(
            f"the configured redirect URI path '{actual}' does not match the "
            f"service mount '{expected}': align service.public_url with the "
            "JupyterHub service name"
        )


def install_shutdown_signals(loop: asyncio.AbstractEventLoop, shutdown: asyncio.Event) -> bool:
    """Wire SIGTERM and SIGINT to the shutdown event, where supported.

    Args:
        loop: The running event loop.
        shutdown: The event a termination signal must set.

    Returns:
        True when the handlers are installed; False on platforms without
        event-loop signal support (Windows development), where console
        interruption falls back to KeyboardInterrupt.
    """
    try:
        loop.add_signal_handler(signal.SIGTERM, shutdown.set)
        loop.add_signal_handler(signal.SIGINT, shutdown.set)
    except (NotImplementedError, RuntimeError):
        return False
    return True


async def serve(shutdown: asyncio.Event | None = None) -> None:
    """Assemble the service from the environment and serve until shutdown.

    Args:
        shutdown: The event that stops the server. When None (the normal
            path), one is created and wired to SIGTERM/SIGINT.

    Raises:
        ServiceStartupError: If the environment or the deployment layout
            prevents startup.
        ConfigError: If the tessera configuration is missing or invalid.
        StoreError: If the token store cannot be opened.
    """
    if shutdown is None:
        shutdown = asyncio.Event()
        install_shutdown_signals(asyncio.get_running_loop(), shutdown)
    config = load_config()
    prefix = _require_env(_ENV_SERVICE_PREFIX)
    _require_env(_ENV_API_TOKEN)
    host, port = _bind_address(_require_env(_ENV_SERVICE_URL))
    _check_callback_path(config, prefix)
    executor = ThreadPoolExecutor(
        max_workers=_EXECUTOR_MAX_WORKERS, thread_name_prefix="tessera-kstlib"
    )
    store = TokenStore(config.store)
    await store.initialize()
    try:
        orchestrator = FlowOrchestrator(config, PendingFlowStore(), store, executor=executor)
        app = make_app(
            orchestrator,
            prefix=prefix,
            hub_auth=HubAuth(base_url=prefix),
            hub_oauth=HubOAuth(base_url=prefix),
            cookie_secret=secrets.token_bytes(32),
        )
        server = app.listen(port, address=host)
        log.info(
            "tessera service listening on %s:%d under prefix '%s' (%d declared server(s))",
            host,
            port,
            prefix,
            len(config.server_names),
        )
        await shutdown.wait()
        log.info("tessera service shutting down")
        server.stop()
        await server.close_all_connections()
    finally:
        await store.close()
        executor.shutdown(wait=True)


def main() -> None:
    """Entry point for ``python -m tessera.service``.

    Configures logging from ``TESSERA_LOG_LEVEL`` (INFO by default, an
    unsupported value falling back to INFO) and runs the server until a
    shutdown signal. A startup problem exits with status 2 and one
    actionable log line.
    """
    raw_level = os.environ.get("TESSERA_LOG_LEVEL", "INFO").strip().upper()
    configure_logging(raw_level if raw_level in _LOG_LEVELS else "INFO")
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        log.info("tessera service interrupted")
    except (ServiceStartupError, ConfigError, StoreError) as exc:
        log.error("startup failed: %s", exc)
        raise SystemExit(2) from exc
