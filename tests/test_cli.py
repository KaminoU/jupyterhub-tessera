"""Smoke tests for the tessera Typer CLI."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import NoReturn

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from tessera import meta
from tessera.cli import _doctor
from tessera.cli import main as cli_main_module
from tessera.cli.app import app
from tessera.config.loader import load_config

runner = CliRunner()


def test_version_flag_prints_version() -> None:
    """``tessera --version`` prints the version and exits zero."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert meta.__version__ in result.output


def test_info_shows_name_and_version() -> None:
    """``tessera info`` shows the package name and version."""
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0
    assert meta.__app_name__ in result.output
    assert meta.__version__ in result.output


def test_info_full_runs_without_installed_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tessera info --full`` falls back when the distribution metadata is absent."""

    def _raise_not_found(name: str) -> NoReturn:
        raise PackageNotFoundError(name)

    app_module = importlib.import_module("tessera.cli.app")
    monkeypatch.setattr(app_module, "metadata", _raise_not_found)
    result = runner.invoke(app, ["info", "--full"])
    assert result.exit_code == 0
    assert meta.__author__ in result.output
    assert "install jupyterhub-tessera to list classifiers" in result.output


def test_info_full_lists_classifiers_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tessera info --full`` lists classifiers when metadata is present."""

    class _FakeMetadata:
        def get_all(self, name: str) -> list[str]:
            return ["License :: OSI Approved :: MIT License"]

    app_module = importlib.import_module("tessera.cli.app")
    monkeypatch.setattr(app_module, "metadata", lambda name: _FakeMetadata())
    result = runner.invoke(app, ["info", "--full"])
    assert result.exit_code == 0
    assert "MIT License" in result.output


def test_config_snippet_raw_emits_the_three_rbac_directives() -> None:
    """``config-snippet --raw`` emits the three RBAC grants (anti-403 regression)."""
    result = runner.invoke(app, ["config-snippet", "--raw"])
    assert result.exit_code == 0
    assert "load_roles" in result.output
    assert "oauth_client_allowed_scopes" in result.output
    assert "server_token_scopes" in result.output


def test_config_snippet_wizard_injects_the_prompted_urls() -> None:
    """The wizard weaves the prompted URLs into the block under the fixed name."""
    result = runner.invoke(
        app,
        ["config-snippet"],
        input="http://127.0.0.1:9999\nhttps://hub.example.net\n",
    )
    assert result.exit_code == 0
    assert "https://hub.example.net/services/tessera/" in result.output
    assert "http://127.0.0.1:9999" in result.output
    assert "access:services!service=tessera" in result.output


def test_config_snippet_never_emits_a_secret_value() -> None:
    """Secrets appear only as environment variable references, never as values."""
    result = runner.invoke(app, ["config-snippet", "--raw"])
    assert result.exit_code == 0
    assert "TESSERA_DB_KEY" in result.output
    assert "TESSERA_CLIENT_SECRET" in result.output
    assert "test-only" not in result.output
    assert "not-a-secret" not in result.output


def test_init_config_wizard_round_trips(tmp_path: Path) -> None:
    """The wizard writes a two-server file that reloads through the loader."""
    target = tmp_path / "servers.yml"
    wizard_input = "".join(
        line + "\n"
        for line in [
            "alpha",  # server name
            "",  # button label (default = name)
            "",  # discovery? default yes
            "https://idp.example.com/realms/one",  # issuer
            "client-one",  # client id
            "",  # secret by env? default yes
            "ALPHA_SECRET",  # env var name
            "",  # scopes (default openid)
            "",  # color valid (default)
            "",  # color invalid (default)
            "",  # auto refresh? default yes
            "y",  # add another server? yes
            "beta",  # server 2 name
            "",  # button label (default)
            "n",  # discovery? no -> endpoints
            "https://idp.example.com/authorize",  # authorization endpoint
            "https://idp.example.com/token",  # token endpoint
            "client-two",  # client id
            "n",  # secret by env? no -> file
            "/etc/tessera/beta.secret",  # secret file path
            "",  # scopes
            "",  # color valid
            "",  # color invalid
            "",  # auto refresh
            "",  # add another? default no
            "/var/lib/tessera/tokens.db",  # store db location
            "",  # key reference (default env)
            "TESSERA_DB_KEY",  # key env var name
            "https://hub.example.org",  # public url
        ]
    )
    result = runner.invoke(app, ["init-config", "--output", str(target)], input=wizard_input)
    assert result.exit_code == 0, result.output
    config = load_config(target)
    assert config.server_names == ("alpha", "beta")
    assert config.get("alpha").provider.issuer == "https://idp.example.com/realms/one"
    assert config.get("alpha").client_secret_env == "ALPHA_SECRET"
    assert config.get("beta").provider.authorization_endpoint == "https://idp.example.com/authorize"
    assert config.get("beta").client_secret_file == "/etc/tessera/beta.secret"
    assert config.store.key_env == "TESSERA_DB_KEY"
    assert config.service.redirect_uri == "https://hub.example.org/services/tessera/callback"
    assert "/services/tessera/callback" in result.output


def test_init_config_raw_round_trips(tmp_path: Path) -> None:
    """``init-config --raw`` writes a valid example that reloads through the loader."""
    target = tmp_path / "servers.yml"
    result = runner.invoke(app, ["init-config", "--raw", "--output", str(target)])
    assert result.exit_code == 0, result.output
    config = load_config(target)
    assert config.server_names == ("demo",)
    assert config.service.redirect_uri.endswith("/services/tessera/callback")


def test_init_config_never_writes_a_secret_value(tmp_path: Path) -> None:
    """The generated file only references secrets; no value is ever written."""
    target = tmp_path / "servers.yml"
    result = runner.invoke(app, ["init-config", "--raw", "--output", str(target)])
    assert result.exit_code == 0, result.output
    content = target.read_text(encoding="utf-8")
    assert "client_secret_env" in content
    assert "key_env" in content
    assert "client_secret:" not in content
    config = load_config(target)
    assert config.get("demo").client_secret_env == "TESSERA_CLIENT_SECRET"
    assert config.get("demo").client_secret_file is None


def test_init_config_refuses_to_overwrite_without_confirmation(tmp_path: Path) -> None:
    """An existing file is preserved when the operator declines the overwrite."""
    target = tmp_path / "servers.yml"
    target.write_text("original: keep-me\n", encoding="utf-8")
    result = runner.invoke(app, ["init-config", "--raw", "--output", str(target)], input="n\n")
    assert result.exit_code != 0
    assert target.read_text(encoding="utf-8") == "original: keep-me\n"


def test_init_config_force_overwrites_without_asking(tmp_path: Path) -> None:
    """``--force`` overwrites an existing file with no confirmation prompt."""
    target = tmp_path / "servers.yml"
    target.write_text("original: replace-me\n", encoding="utf-8")
    result = runner.invoke(app, ["init-config", "--raw", "--force", "--output", str(target)])
    assert result.exit_code == 0, result.output
    assert load_config(target).server_names == ("demo",)


def test_init_config_overwrite_confirmed_replaces_the_file(tmp_path: Path) -> None:
    """Confirming the overwrite replaces the existing file."""
    target = tmp_path / "servers.yml"
    target.write_text("original: replace-me\n", encoding="utf-8")
    result = runner.invoke(app, ["init-config", "--raw", "--output", str(target)], input="y\n")
    assert result.exit_code == 0, result.output
    assert load_config(target).server_names == ("demo",)


@pytest.mark.parametrize(
    ("kind", "value", "attr"),
    [
        ("file", "/etc/tessera/db.key", "key_file"),
        ("sops", "/etc/tessera/db.sops.yaml", "key_sops"),
    ],
)
def test_init_config_store_key_reference_variants(
    tmp_path: Path, kind: str, value: str, attr: str
) -> None:
    """The store key can be referenced by file or SOPS, never by value."""
    target = tmp_path / "servers.yml"
    wizard_input = "".join(
        line + "\n"
        for line in [
            "solo",  # server name
            "",  # label
            "",  # discovery yes
            "https://idp.example.com/realms/solo",  # issuer
            "solo-client",  # client id
            "",  # secret env yes
            "SOLO_SECRET",  # env name
            "",  # scopes
            "",  # color valid
            "",  # color invalid
            "",  # auto refresh
            "",  # add another? no
            "/var/lib/tessera/tokens.db",  # store db
            kind,  # key kind (file/sops)
            value,  # key path
            "https://hub.example.org",  # public url
        ]
    )
    result = runner.invoke(app, ["init-config", "--output", str(target)], input=wizard_input)
    assert result.exit_code == 0, result.output
    assert getattr(load_config(target).store, attr) == value


_ISSUER = "https://idp.example.com/realms/demo"
_Handler = Callable[[httpx.Request], httpx.Response]

_DISCOVERY_OK = {
    "authorization_endpoint": "https://idp.example.com/authorize",
    "token_endpoint": "https://idp.example.com/token",
}

_DISCOVERY_RICH = {
    "issuer": "https://idp.example.com",
    "authorization_endpoint": "https://idp.example.com/authorize",
    "token_endpoint": "https://idp.example.com/token",
    "jwks_uri": "https://idp.example.com/jwks",
    "scopes_supported": ["openid", "profile"],
}


def _write_doctor_config(
    target: Path,
    *,
    discovery: bool = True,
    issuer: str = _ISSUER,
    public_url: str = "https://hub.example.org",
) -> None:
    """Write a valid single-server configuration for the doctor tests."""
    provider: dict[str, str] = {"issuer": issuer} if discovery else dict(_DISCOVERY_OK)
    data: dict[str, object] = {
        "servers": {
            "demo": {
                "provider": provider,
                "client_id": "demo-client",
                "client_secret_env": "DOCTOR_SECRET",
                "scopes": ["openid"],
            }
        },
        "store": {"db_location": "/var/lib/tessera/tokens.db", "key_env": "DOCTOR_DB_KEY"},
        "service": {"public_url": public_url},
    }
    target.write_text(yaml.safe_dump(data), encoding="utf-8")


def _install_probe(monkeypatch: pytest.MonkeyPatch, handler: _Handler) -> None:
    """Route the doctor discovery probe through a mock transport (no network)."""
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(_doctor, "_new_client", lambda: httpx.Client(transport=transport))


def test_doctor_lists_servers_without_running_checks(tmp_path: Path) -> None:
    """``doctor --list-servers`` lists names and labels and runs no checks."""
    target = tmp_path / "servers.yml"
    _write_doctor_config(target)
    result = runner.invoke(app, ["doctor", "--list-servers", "--config", str(target)])
    assert result.exit_code == 0, result.output
    assert "demo" in result.output


def test_doctor_unknown_server_is_actionable(tmp_path: Path) -> None:
    """An unknown --server name names the resolved path and the available servers."""
    target = tmp_path / "servers.yml"
    _write_doctor_config(target)
    result = runner.invoke(app, ["doctor", "--server", "ghost", "--config", str(target)])
    assert result.exit_code != 0
    assert "not declared" in result.output
    assert "demo" in result.output


def test_doctor_missing_config_is_graceful(tmp_path: Path) -> None:
    """A missing configuration file yields an actionable message, not a traceback."""
    target = tmp_path / "absent.yml"
    result = runner.invoke(app, ["doctor", "--config", str(target)])
    assert result.exit_code != 0
    assert "cannot read configuration" in result.output


def test_doctor_refuses_a_public_url_that_is_not_a_url(tmp_path: Path) -> None:
    """A ``public_url`` carrying an unexpanded shell variable is refused, not reported ok.

    ``--summary`` restricts the run to the rows read from the configuration, so
    the non-zero exit can only come from the configuration itself.
    """
    target = tmp_path / "servers.yml"
    _write_doctor_config(target, public_url="https://$\\{SRV\\}:9999")
    result = runner.invoke(app, ["doctor", "--offline", "--summary", "--config", str(target)])
    assert result.exit_code != 0, result.output
    assert "public_url" in result.output
    assert "Traceback" not in result.output


def test_doctor_offline_skips_the_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--offline`` reports the discovery check as skipped and runs no network."""
    target = tmp_path / "servers.yml"
    _write_doctor_config(target)
    monkeypatch.setenv("DOCTOR_SECRET", "present")
    result = runner.invoke(app, ["doctor", "--offline", "--config", str(target)])
    assert result.exit_code == 0, result.output
    assert "skipped (offline)" in result.output


def test_doctor_offline_wins_over_well_known(tmp_path: Path) -> None:
    """``--offline`` and ``--well-known`` together skip the probe (offline wins)."""
    target = tmp_path / "servers.yml"
    _write_doctor_config(target)
    result = runner.invoke(app, ["doctor", "--offline", "--well-known", "--config", str(target)])
    assert result.exit_code == 0, result.output
    assert "skipped (offline)" in result.output


def test_doctor_well_known_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable discovery document with both endpoints passes the probe."""
    target = tmp_path / "servers.yml"
    _write_doctor_config(target)
    _install_probe(monkeypatch, lambda request: httpx.Response(200, json=_DISCOVERY_OK))
    result = runner.invoke(app, ["doctor", "--well-known", "--config", str(target)])
    assert result.exit_code == 0, result.output
    assert "reachable" in result.output


@pytest.mark.parametrize(
    ("handler", "fragment"),
    [
        (lambda request: httpx.Response(500), "HTTP 500"),
        (lambda request: httpx.Response(200, text="not json"), "not valid JSON"),
        (lambda request: httpx.Response(200, json={"issuer": "x"}), "missing"),
    ],
)
def test_doctor_well_known_failures_exit_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handler: _Handler, fragment: str
) -> None:
    """Unreachable, unparseable, or incomplete discovery documents fail the probe."""
    target = tmp_path / "servers.yml"
    _write_doctor_config(target)
    _install_probe(monkeypatch, handler)
    result = runner.invoke(app, ["doctor", "--well-known", "--config", str(target)])
    assert result.exit_code != 0
    assert fragment in result.output


def test_doctor_well_known_transport_error_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport error surfaces as an unreachable failure, not a traceback."""
    target = tmp_path / "servers.yml"
    _write_doctor_config(target)

    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    _install_probe(monkeypatch, _boom)
    result = runner.invoke(app, ["doctor", "--well-known", "--config", str(target)])
    assert result.exit_code != 0
    assert "unreachable" in result.output


def test_doctor_secret_not_resolvable_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unset client-secret env var fails the check, naming the reference."""
    target = tmp_path / "servers.yml"
    _write_doctor_config(target)
    monkeypatch.delenv("DOCTOR_SECRET", raising=False)
    result = runner.invoke(app, ["doctor", "--secret", "--config", str(target)])
    assert result.exit_code != 0
    assert "DOCTOR_SECRET" in result.output


def test_doctor_secret_resolves_without_revealing_the_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A set client-secret env var passes the check without printing its value."""
    target = tmp_path / "servers.yml"
    _write_doctor_config(target)
    monkeypatch.setenv("DOCTOR_SECRET", "super-secret-value")
    result = runner.invoke(app, ["doctor", "--secret", "--config", str(target)])
    assert result.exit_code == 0, result.output
    assert "super-secret-value" not in result.output


def test_doctor_explicit_endpoints_skip_well_known(tmp_path: Path) -> None:
    """A server with explicit endpoints has no discovery document to probe."""
    target = tmp_path / "servers.yml"
    _write_doctor_config(target, discovery=False)
    result = runner.invoke(app, ["doctor", "--well-known", "--config", str(target)])
    assert result.exit_code == 0, result.output
    assert "no discovery" in result.output


def test_doctor_single_server_full_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--server`` scopes to one server and runs the full report."""
    target = tmp_path / "servers.yml"
    _write_doctor_config(target)
    monkeypatch.setenv("DOCTOR_SECRET", "present")
    _install_probe(monkeypatch, lambda request: httpx.Response(200, json=_DISCOVERY_OK))
    result = runner.invoke(app, ["doctor", "--server", "demo", "--config", str(target)])
    assert result.exit_code == 0, result.output
    assert "/services/tessera/callback" in result.output
    assert "reference resolves" in result.output


def test_doctor_summary_only_lists_config_rows(tmp_path: Path) -> None:
    """``--summary`` shows the configuration rows and runs no network or secret check."""
    target = tmp_path / "servers.yml"
    _write_doctor_config(target)
    result = runner.invoke(app, ["doctor", "--summary", "--config", str(target)])
    assert result.exit_code == 0, result.output
    assert "provider" in result.output
    assert "redirect_uri" in result.output
    assert "reference resolves" not in result.output
    assert "reachable" not in result.output


def test_doctor_verbose_shows_discovered_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``-v`` on a successful probe shows the probed URL and the discovered fields."""
    target = tmp_path / "servers.yml"
    _write_doctor_config(target, issuer="https://idp.example.com")
    monkeypatch.setenv("COLUMNS", "200")  # wide render so the probed URL is not line-folded
    _install_probe(monkeypatch, lambda request: httpx.Response(200, json=_DISCOVERY_RICH))
    result = runner.invoke(app, ["doctor", "--well-known", "-v", "--config", str(target)])
    assert result.exit_code == 0, result.output
    assert "/.well-known/openid-configuration" in result.output
    assert "jwks_uri" in result.output
    assert "https://idp.example.com/jwks" in result.output
    assert "openid, profile" in result.output


def test_doctor_verbose_explicit_endpoints_still_skip(tmp_path: Path) -> None:
    """``-v`` on an explicit-endpoints server still skips (nothing to discover)."""
    target = tmp_path / "servers.yml"
    _write_doctor_config(target, discovery=False)
    result = runner.invoke(app, ["doctor", "--well-known", "-v", "--config", str(target)])
    assert result.exit_code == 0, result.output
    assert "no discovery" in result.output


def test_install_kernel_config_writes_the_autoload_file(tmp_path: Path) -> None:
    """The command writes an ipython_config.py that appends the tessera extension."""
    target = tmp_path / "ipython"
    result = runner.invoke(app, ["install-kernel-config", "--target", str(target)])
    assert result.exit_code == 0, result.output
    written = target / "ipython_config.py"
    assert written.exists()
    assert 'extensions.append("tessera.kernel")' in written.read_text(encoding="utf-8")


def test_install_kernel_config_refuses_to_overwrite(tmp_path: Path) -> None:
    """An existing config file is preserved, not clobbered, and the command fails."""
    target = tmp_path / "ipython"
    target.mkdir()
    existing = target / "ipython_config.py"
    existing.write_text("# keep me\n", encoding="utf-8")
    result = runner.invoke(app, ["install-kernel-config", "--target", str(target)])
    assert result.exit_code != 0
    assert "refusing to overwrite" in result.output
    assert existing.read_text(encoding="utf-8") == "# keep me\n"


def test_install_kernel_config_unwritable_target_is_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory that cannot be created yields an actionable error, not a traceback."""
    target = tmp_path / "ipython"

    def _deny(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "mkdir", _deny)
    result = runner.invoke(app, ["install-kernel-config", "--target", str(target)])
    assert result.exit_code != 0
    assert "cannot write" in result.output


def test_main_delegates_to_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main()`` runs the Typer application."""
    calls: list[bool] = []
    monkeypatch.setattr("tessera.cli.main.app", lambda: calls.append(True))
    cli_main_module.main()
    assert calls == [True]
