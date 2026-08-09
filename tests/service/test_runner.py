"""Tests for the service runner: environment contract and lifecycle.

The runner is exercised through its public surface (``serve``, ``main``,
``install_shutdown_signals``): every deployment mistake must fail fast
with an actionable error before the server binds, and a normal lifecycle
must assemble, listen, and release every resource.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest
from tornado.testing import bind_unused_port

import tessera.service.runner as runner_module
from tessera.service.runner import (
    ServiceStartupError,
    configure_logging,
    install_shutdown_signals,
    main,
    serve,
)

_SECRET_ENV = "TESSERA_TEST_RUNNER_SECRET"

_MANAGED_LOGGERS = ("tessera", "httpx", "kstlib.config.loader", "kstlib.auth")


@pytest.fixture(autouse=True)
def _restore_logging_state() -> Iterator[None]:
    """Restore the loggers ``configure_logging`` mutates around each test.

    ``configure_logging`` and ``main`` reconfigure logging as a
    process-wide side effect (a dedicated handler on the ``tessera``
    logger with propagation disabled, plus quieted third-party loggers).
    Confining the restore to this module keeps that side effect from
    leaking into ``caplog`` or level expectations of any other test.
    """
    snapshot = [
        (logger, list(logger.handlers), logger.level, logger.propagate)
        for logger in (logging.getLogger(name) for name in _MANAGED_LOGGERS)
    ]
    try:
        yield
    finally:
        for logger, handlers, level, propagate in snapshot:
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate = propagate


_CONFIG_TEMPLATE = """\
servers:
  manual-idp:
    provider:
      authorization_endpoint: https://idp.example.com/authorize
      token_endpoint: https://idp.example.com/token
    client_id: tessera-demo-client
    client_secret_env: {secret_env}
    scopes: [openid]
store:
  db_location: {db_location}
  key_file: {key_file}
service:
  public_url: https://hub.example.com
"""


def _free_port() -> int:
    """Reserve an unused local port and release it for the runner."""
    sock, port = bind_unused_port()
    sock.close()
    return port


@pytest.fixture
def runner_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a complete, valid runner environment on disk.

    Args:
        tmp_path: The per-test directory for the config, key, and db.
        monkeypatch: The environment patcher.
    """
    key_path = tmp_path / "db.key"
    key_path.write_text("a-strong-test-passphrase-for-the-store", encoding="utf-8")
    key_path.chmod(0o600)
    config_path = tmp_path / "servers.yml"
    config_path.write_text(
        _CONFIG_TEMPLATE.format(
            secret_env=_SECRET_ENV,
            db_location=(tmp_path / "tokens.db").as_posix(),
            key_file=key_path.as_posix(),
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TESSERA_CONFIG", str(config_path))
    monkeypatch.setenv(_SECRET_ENV, "a-very-confidential-client-secret-value")
    monkeypatch.setenv("JUPYTERHUB_API_TOKEN", "tessera-service-api-token")
    monkeypatch.setenv("JUPYTERHUB_CLIENT_ID", "service-tessera")
    monkeypatch.setenv("JUPYTERHUB_SERVICE_PREFIX", "/services/tessera/")
    monkeypatch.setenv("JUPYTERHUB_SERVICE_URL", f"http://127.0.0.1:{_free_port()}")


class TestServeLifecycle:
    """A valid environment assembles, listens, and shuts down cleanly."""

    async def test_serve_starts_and_stops(self, runner_env: None) -> None:
        """With the shutdown pre-set, serve runs one full lifecycle."""
        shutdown = asyncio.Event()
        shutdown.set()
        await serve(shutdown)


class TestServeEnvironmentContract:
    """Every deployment mistake fails fast, before the server binds."""

    async def test_missing_prefix_is_rejected(
        self, runner_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The service prefix is part of the environment contract."""
        monkeypatch.delenv("JUPYTERHUB_SERVICE_PREFIX")
        with pytest.raises(ServiceStartupError, match="JUPYTERHUB_SERVICE_PREFIX"):
            await serve(asyncio.Event())

    async def test_missing_api_token_is_rejected(
        self, runner_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Hub API token must be present for the authenticators."""
        monkeypatch.delenv("JUPYTERHUB_API_TOKEN")
        with pytest.raises(ServiceStartupError, match="JUPYTERHUB_API_TOKEN"):
            await serve(asyncio.Event())

    async def test_https_bind_is_rejected(
        self, runner_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The service never terminates TLS itself."""
        monkeypatch.setenv("JUPYTERHUB_SERVICE_URL", "https://127.0.0.1:8443")
        with pytest.raises(ServiceStartupError, match="https"):
            await serve(asyncio.Event())

    async def test_missing_port_is_rejected(
        self, runner_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An implicit port is refused: the bind must be explicit."""
        monkeypatch.setenv("JUPYTERHUB_SERVICE_URL", "http://127.0.0.1")
        with pytest.raises(ServiceStartupError, match="explicit port"):
            await serve(asyncio.Event())

    async def test_garbage_port_is_rejected(
        self, runner_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unparsable port is refused with the same actionable error."""
        monkeypatch.setenv("JUPYTERHUB_SERVICE_URL", "http://127.0.0.1:notaport")
        with pytest.raises(ServiceStartupError, match="explicit port"):
            await serve(asyncio.Event())

    async def test_non_http_scheme_is_rejected(
        self, runner_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only http binds are supported."""
        monkeypatch.setenv("JUPYTERHUB_SERVICE_URL", "ftp://127.0.0.1:2121")
        with pytest.raises(ServiceStartupError, match="http URL"):
            await serve(asyncio.Event())

    async def test_callback_path_mismatch_is_rejected(
        self, runner_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A redirect URI that the mount cannot serve is a startup error."""
        monkeypatch.setenv("JUPYTERHUB_SERVICE_PREFIX", "/services/other/")
        with pytest.raises(ServiceStartupError, match="redirect URI"):
            await serve(asyncio.Event())


class TestMain:
    """The entry point exits with an actionable status."""

    def test_startup_error_exits_2(self, runner_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        """A broken environment exits with status 2."""
        monkeypatch.delenv("JUPYTERHUB_SERVICE_PREFIX")
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 2

    def test_missing_configuration_exits_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing tessera configuration exits with status 2."""
        monkeypatch.setenv("TESSERA_CONFIG", str(tmp_path / "absent.yml"))
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 2

    def test_keyboard_interrupt_is_a_clean_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Console interruption stops the service without a traceback."""

        async def interrupted(shutdown: asyncio.Event | None = None) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(runner_module, "serve", interrupted)
        main()


class TestLogging:
    """Logging is greppable, level-controlled, and quiets third-party noise."""

    def test_format_is_greppable(self) -> None:
        """A record renders in the exact bracketed tessera format."""
        formatter = logging.Formatter(runner_module.LOG_FORMAT)
        record = logging.LogRecord("tessera.demo", logging.INFO, "path.py", 1, "hello", None, None)
        formatted = formatter.format(record)
        assert re.fullmatch(r"\[TESSERA ::: .+ ::: INFO\] tessera\.demo: hello", formatted)

    def test_configure_logging_wires_the_tessera_logger(self) -> None:
        """The tessera logger gets one formatted handler and stops propagating."""
        configure_logging("DEBUG")
        logger = logging.getLogger("tessera")
        assert logger.level == logging.DEBUG
        assert logger.propagate is False
        assert len(logger.handlers) == 1

    def test_configure_logging_is_idempotent(self) -> None:
        """Reconfiguring never stacks duplicate handlers."""
        configure_logging("INFO")
        configure_logging("INFO")
        assert len(logging.getLogger("tessera").handlers) == 1

    def test_third_party_loggers_are_quieted(self) -> None:
        """httpx, the kstlib config loader, and kstlib.auth are pinned to WARNING."""
        logging.getLogger("httpx").setLevel(logging.DEBUG)
        logging.getLogger("kstlib.config.loader").setLevel(logging.DEBUG)
        logging.getLogger("kstlib.auth").setLevel(logging.DEBUG)
        configure_logging("DEBUG")
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("kstlib.config.loader").level == logging.WARNING
        assert logging.getLogger("kstlib.auth").level == logging.WARNING

    def test_valid_level_from_env_is_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A supported TESSERA_LOG_LEVEL reaches the tessera logger."""
        monkeypatch.setenv("TESSERA_LOG_LEVEL", "WARNING")

        async def noop(shutdown: asyncio.Event | None = None) -> None:
            return None

        monkeypatch.setattr(runner_module, "serve", noop)
        main()
        assert logging.getLogger("tessera").level == logging.WARNING

    def test_invalid_level_falls_back_to_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unsupported TESSERA_LOG_LEVEL falls back to INFO, never crashes."""
        monkeypatch.setenv("TESSERA_LOG_LEVEL", "BOGUS")

        async def noop(shutdown: asyncio.Event | None = None) -> None:
            return None

        monkeypatch.setattr(runner_module, "serve", noop)
        main()
        assert logging.getLogger("tessera").level == logging.INFO


class TestSignalWiring:
    """Signal handlers are installed where the platform supports them."""

    def test_handlers_installed_when_supported(self) -> None:
        """Both termination signals are wired to the shutdown event."""
        loop = mock.Mock()
        shutdown = asyncio.Event()
        assert install_shutdown_signals(loop, shutdown) is True
        assert loop.add_signal_handler.call_count == 2

    def test_windows_fallback_reports_false(self) -> None:
        """Platforms without loop signals fall back to KeyboardInterrupt."""
        loop = mock.Mock()
        loop.add_signal_handler.side_effect = NotImplementedError
        assert install_shutdown_signals(loop, asyncio.Event()) is False


class TestModuleSurface:
    """The runnable module and the package exports stay wired."""

    def test_dunder_main_is_runnable(self) -> None:
        """``python -m tessera.service`` resolves to the runner main."""
        module = importlib.import_module("tessera.service.__main__")
        assert callable(module.main)

    def test_package_exports(self) -> None:
        """The service package exports its public surface."""
        from tessera.service import main as exported_main
        from tessera.service import make_app

        assert callable(exported_main)
        assert callable(make_app)
