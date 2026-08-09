"""Health checks for a tessera deployment: config validity and connectivity.

The ``doctor`` command loads the server configuration, then for each server
probes the OAuth provider (the OIDC discovery document over HTTP) and checks
that the referenced client secret resolves. It is strictly read-only: it
never writes, never refreshes a token, and never prints a secret value (the
resolved value is discarded; only the reference is ever named).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import typer
from kstlib.ui import PanelManager, Spinner, TableBuilder

from tessera.cli._helpers import _fail
from tessera.config.errors import ConfigFileError, ConfigValidationError
from tessera.config.loader import load_config, resolve_config_path
from tessera.config.models import ServerConfig, TesseraConfig
from tessera.flow.errors import SecretResolutionError
from tessera.flow.secrets import resolve_client_secret

_HTTP_TIMEOUT = 10.0
_WELL_KNOWN_PATH = "/.well-known/openid-configuration"

# Curated OIDC metadata surfaced by --verbose, in priority order. The bulk
# crypto-algorithm lists are deliberately excluded: the raw document is one
# curl away and does not belong in a health report.
_VERBOSE_FIELDS = (
    "issuer",
    "authorization_endpoint",
    "token_endpoint",
    "jwks_uri",
    "userinfo_endpoint",
    "end_session_endpoint",
    "revocation_endpoint",
    "registration_endpoint",
    "device_authorization_endpoint",
    "code_challenge_methods_supported",
    "grant_types_supported",
    "scopes_supported",
    "response_types_supported",
)

_panels = PanelManager()
_tables = TableBuilder()


@dataclass(frozen=True)
class _Check:
    """One check outcome shown in the report.

    ``status`` is one of ``"ok"``, ``"fail"``, or ``"skip"``; only a
    ``"fail"`` makes the command exit non-zero. ``fields`` holds the curated
    discovery metadata rendered under a successful verbose probe.
    """

    label: str
    status: str
    detail: str
    fields: tuple[tuple[str, str], ...] = ()


def _new_client() -> httpx.Client:
    """Build the HTTP client for provider probes.

    Isolated in one function so a test can inject a mock transport with no
    network round-trip. The timeout is mandatory: a hung provider must never
    block the command indefinitely.
    """
    return httpx.Client(timeout=_HTTP_TIMEOUT)


def _discovered_fields(document: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Curate discovery metadata for the verbose report.

    Keeps the endpoint and capability fields listed in ``_VERBOSE_FIELDS``, in
    that order, omitting any absent from the document and joining list values
    with ``", "``.
    """
    pairs: list[tuple[str, str]] = []
    for key in _VERBOSE_FIELDS:
        if key not in document:
            continue
        value = document[key]
        text = ", ".join(str(item) for item in value) if isinstance(value, list) else str(value)
        pairs.append((key, text))
    return tuple(pairs)


def _well_known_check(response: httpx.Response, *, url: str, verbose: bool) -> _Check:
    """Turn a discovery-document response into a well-known check outcome.

    On success with ``verbose`` set, the detail is the probed URL and the
    curated discovered fields are attached for rendering.
    """
    if response.status_code != 200:
        return _Check("well-known", "fail", f"HTTP {response.status_code}")
    try:
        document = response.json()
    except ValueError:
        return _Check("well-known", "fail", "response is not valid JSON")
    if (
        not isinstance(document, dict)
        or "authorization_endpoint" not in document
        or "token_endpoint" not in document
    ):
        return _Check("well-known", "fail", "missing authorization or token endpoint")
    if verbose:
        return _Check("well-known", "ok", url, _discovered_fields(document))
    return _Check("well-known", "ok", "reachable, endpoints present")


def _probe_well_known(
    server: ServerConfig, *, offline: bool, client: httpx.Client, verbose: bool
) -> _Check:
    """Probe the OIDC discovery document, spinning during the network call.

    The spinner is stopped in a ``finally``: no exit path, expected or not,
    can leave the terminal line spinning.
    """
    issuer = server.provider.issuer
    if issuer is None:
        return _Check("well-known", "skip", "explicit endpoints (no discovery)")
    if offline:
        return _Check("well-known", "skip", "skipped (offline)")
    url = issuer.rstrip("/") + _WELL_KNOWN_PATH
    spinner = Spinner(f"probing {server.name}")
    spinner.start()
    success = False
    try:
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            check = _Check("well-known", "fail", f"unreachable ({type(exc).__name__})")
        else:
            check = _well_known_check(response, url=url, verbose=verbose)
        success = check.status == "ok"
        return check
    finally:
        spinner.stop(success=success)


def _check_secret(server: ServerConfig) -> _Check:
    """Check the client-secret reference resolves, never revealing the value."""
    try:
        resolve_client_secret(server)
    except SecretResolutionError as exc:
        return _Check("secret", "fail", str(exc))
    return _Check("secret", "ok", "reference resolves")


def _summary_checks(server: ServerConfig, config: TesseraConfig) -> list[_Check]:
    """Informational rows read from the already-validated configuration."""
    mode = "discovery" if server.provider.uses_discovery else "explicit endpoints"
    return [
        _Check("provider", "ok", mode),
        _Check("client_id", "ok", "declared"),
        _Check("scopes", "ok", " ".join(server.scopes)),
        _Check("redirect_uri", "ok", config.service.redirect_uri),
    ]


def _selected(*, well_known: bool, secret: bool, summary: bool) -> tuple[bool, bool, bool]:
    """Resolve the check selectors: none given means run every check."""
    if not (well_known or secret or summary):
        return True, True, True
    return summary, well_known, secret


def _check_server(
    server: ServerConfig,
    config: TesseraConfig,
    *,
    offline: bool,
    client: httpx.Client,
    selectors: tuple[bool, bool, bool],
    verbose: bool,
) -> list[_Check]:
    """Run the selected checks for one server."""
    run_summary, run_well_known, run_secret = selectors
    checks: list[_Check] = []
    if run_summary:
        checks.extend(_summary_checks(server, config))
    if run_well_known:
        checks.append(_probe_well_known(server, offline=offline, client=client, verbose=verbose))
    if run_secret:
        checks.append(_check_secret(server))
    return checks


def _render(server: ServerConfig, checks: list[_Check]) -> None:
    """Render one server's checks as a table, plus verbose discovery fields."""
    _tables.print_table(
        rows=[[check.label, check.status, check.detail] for check in checks],
        columns=[{"header": "check"}, {"header": "status"}, {"header": "detail"}],
        title=f"server '{server.name}'",
    )
    for check in checks:
        if check.fields:
            _panels.print_panel("info", payload=list(check.fields), use_markup=False)


def _list_servers(config: TesseraConfig) -> None:
    """List the declared servers with their labels, running no checks."""
    _tables.print_table(
        rows=[[name, config.get(name).button.label] for name in config.server_names],
        columns=[{"header": "server"}, {"header": "label"}],
        title="declared servers",
    )


def _select_servers(config: TesseraConfig, name: str | None, resolved: Path) -> list[ServerConfig]:
    """Return the servers to check, or fail actionably on an unknown name."""
    if name is None:
        return [config.get(server) for server in config.server_names]
    if name not in config.server_names:
        available = ", ".join(config.server_names)
        _fail(f"server '{name}' not declared in {resolved}. Available: {available}")
    return [config.get(name)]


def run(
    *,
    offline: bool,
    config: Path | None,
    server: str | None,
    list_servers: bool,
    well_known: bool,
    secret: bool,
    summary: bool,
    verbose: bool,
) -> None:
    """Load the configuration and run the selected health checks.

    Args:
        offline: When True, skip every network check (config and secret only).
        config: Explicit config path; defaults to the loader cascade location.
        server: When set, restrict the checks to that one server.
        list_servers: When True, list the declared servers and exit.
        well_known: When True, run only the discovery probe.
        secret: When True, run only the secret-resolution check.
        summary: When True, run only the configuration summary.
        verbose: When True, show discovered endpoints on a successful probe.
    """
    resolved = resolve_config_path(config)
    try:
        loaded = load_config(resolved)
    except (ConfigFileError, ConfigValidationError) as exc:
        _fail(f"cannot read configuration at {resolved}: {exc}")
    if list_servers:
        _list_servers(loaded)
        return
    servers = _select_servers(loaded, server, resolved)
    selectors = _selected(well_known=well_known, secret=secret, summary=summary)
    client = _new_client()
    failed = False
    try:
        for target in servers:
            checks = _check_server(
                target, loaded, offline=offline, client=client, selectors=selectors, verbose=verbose
            )
            _render(target, checks)
            failed = failed or any(check.status == "fail" for check in checks)
    finally:
        client.close()
    if failed:
        raise typer.Exit(1)
