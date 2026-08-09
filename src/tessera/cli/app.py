"""tessera command-line interface built with Typer and Rich.

The administrative CLI is the deployment and operations surface for tessera:
the ``info``, ``config-snippet``, ``init-config``, ``doctor`` and
``install-kernel-config`` commands, plus the ``--version`` flag. Structured
output goes through the kstlib.ui helpers so the CLI shares one house style.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, metadata
from pathlib import Path
from string import Template

import typer
from kstlib.ui import PanelManager
from rich.console import Console

from tessera import meta
from tessera.cli import _doctor, _init_config, _install_kernel_config

console = Console()
_panels = PanelManager(console=console)

app = typer.Typer(add_completion=False, name=meta.__app_name__, no_args_is_help=True)

_SNIPPET_TEMPLATE = Template(
    """\
# tessera as a Hub-managed OAuth service. Paste into your jupyterhub_config.py.
# Requires 'import os' and 'import sys' at the top of that file.

c.JupyterHub.services = [
    {
        "name": "tessera",
        "url": "$bind_url",
        "command": [sys.executable, "-m", "tessera.service"],
        "oauth_no_confirm": True,
        "environment": {
            "TESSERA_CONFIG": "$config_path",
            # Secrets by reference: set these in the service environment,
            # never inline. TESSERA_DB_KEY encrypts the token store at rest;
            # TESSERA_CLIENT_SECRET is the OAuth client secret.
            # "TESSERA_DB_KEY": os.environ["TESSERA_DB_KEY"],
            # "TESSERA_CLIENT_SECRET": os.environ["TESSERA_CLIENT_SECRET"],
            # Optional: raise the log level without editing this file.
            # "TESSERA_LOG_LEVEL": os.environ.get("TESSERA_LOG_LEVEL", "INFO"),
        },
    },
]

# RBAC: without these three grants, every request to the service 403s.
c.JupyterHub.load_roles = [
    {"name": "user", "scopes": ["self", "access:services!service=tessera"]},
]
c.Spawner.oauth_client_allowed_scopes = ["access:services!service=tessera"]
c.Spawner.server_token_scopes = [
    "users:activity!user",
    "access:servers!server",
    "access:services!service=tessera",
]

# Single-user server environment: the kernel-side client reads TESSERA_URL.
# Replace the public URL with your Hub's externally reachable base.
c.Spawner.environment = {
    "TESSERA_URL": "$public_url/services/tessera/",
}

# To auto-load the tessera kernel client in notebooks, see the tessera docs
# (kernel client auto-load via the IPython startup configuration).
"""
)


def _version_callback(value: bool) -> None:
    """Print the tessera version and exit when ``--version`` is given.

    Args:
        value: True when the ``--version`` flag was supplied.

    Raises:
        typer.Exit: Always raised after printing, to stop further processing.
    """
    if value:
        console.print(meta.__version__)
        raise typer.Exit


def _read_classifiers() -> list[str]:
    """Return the PyPI classifiers declared for the installed package.

    The classifiers live in ``pyproject.toml``; reading them from the
    installed metadata avoids duplicating the list in this module. When
    tessera runs from a source checkout that is not installed, the metadata
    is unavailable and an empty list is returned.

    Returns:
        The ``Classifier`` entries, or an empty list when tessera is not
        installed as a distribution.
    """
    try:
        return list(metadata(meta.__dist_name__).get_all("Classifier") or [])
    except PackageNotFoundError:
        return []


@app.callback()
def main_callback(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the tessera version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """tessera administrative CLI (deployment and operations surface)."""


@app.command()
def info(
    full: bool = typer.Option(
        False,
        "--full",
        "-f",
        help="Show the full package metadata.",
    ),
) -> None:
    """Display tessera package information.

    Args:
        full: When True, also show author, keywords and PyPI classifiers.
    """
    fields: dict[str, str] = {
        "Name": meta.__app_name__,
        "Version": meta.__version__,
        "Description": meta.__description__,
        "URL": meta.__url__,
    }
    if full:
        fields["Author"] = meta.__author__
        fields["Keywords"] = ", ".join(meta.__keywords__)
        classifiers = _read_classifiers()
        fields["Classifiers"] = (
            "\n".join(classifiers)
            if classifiers
            else "(install jupyterhub-tessera to list classifiers)"
        )
    _panels.print_panel("info", payload=fields)


@app.command(name="config-snippet")
def config_snippet(
    raw: bool = typer.Option(
        False,
        "--raw",
        help="Emit the block with placeholder values instead of prompting.",
    ),
) -> None:
    """Emit a jupyterhub_config.py block wiring tessera as a Hub service.

    The block mirrors the reference setup, generified: the service
    definition, the three RBAC grants that keep the service from answering
    403, and the single-user ``TESSERA_URL``. The service name is fixed to
    ``tessera`` because the OAuth callback path is not configurable. Secrets
    are referenced by environment variable name only, never by value.
    Read-only; nothing is written to disk.

    Args:
        raw: When True, emit placeholder values and skip the prompts, so the
            block can be piped straight to a file.
    """
    if raw:
        bind_url = "http://127.0.0.1:10101"
        public_url = "https://hub.example.org"
    else:
        bind_url = typer.prompt("Service bind URL", default="http://127.0.0.1:10101")
        public_url = typer.prompt("Hub public URL (for example https://hub.example.org)")
    block = _SNIPPET_TEMPLATE.substitute(
        bind_url=bind_url,
        public_url=public_url,
        config_path="/etc/tessera/servers.yml",
    )
    typer.echo(block)


@app.command(name="init-config")
def init_config(
    raw: bool = typer.Option(
        False, "--raw", help="Write a valid example template instead of prompting."
    ),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Target file (default: the loader cascade location)."
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing file without confirmation."
    ),
) -> None:
    """Scaffold a per-server ``servers.yml`` configuration.

    Runs an interactive wizard (or writes a valid example with ``--raw``)
    collecting the OAuth servers, the token store, and the service URL, then
    writes a file that reloads cleanly through the loader. Secrets are only
    ever referenced (an env var name or a file path), never written as a
    value. Prints the redirect URI to register at the provider.

    Args:
        raw: When True, write an example template and skip the prompts.
        output: Explicit target path; defaults to the config cascade location.
        force: When True, overwrite an existing file without confirmation.
    """
    _init_config.run(raw=raw, output=output, force=force)


@app.command()
def doctor(
    offline: bool = typer.Option(
        False, "--offline", help="Skip network checks (config and secret only)."
    ),
    server: str | None = typer.Option(None, "--server", help="Check only this server."),
    list_servers: bool = typer.Option(
        False, "--list-servers", help="List declared servers and exit."
    ),
    well_known: bool = typer.Option(False, "--well-known", help="Run only the discovery probe."),
    secret: bool = typer.Option(False, "--secret", help="Run only the secret-resolution check."),
    summary: bool = typer.Option(False, "--summary", help="Run only the configuration summary."),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show discovered endpoints on a successful probe."
    ),
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Config file (default: the loader cascade location)."
    ),
) -> None:
    """Check the tessera deployment health.

    Loads the configuration and, per server, probes the OIDC discovery
    document and checks the client-secret reference resolves. Read-only:
    nothing is written and no secret value is ever printed. Exits non-zero
    when a check fails, so it is usable in CI. ``--offline`` skips the
    network; ``--server`` scopes to one; ``--list-servers`` lists and exits;
    ``--well-known``/``--secret``/``--summary`` select checks (default all);
    ``--verbose`` adds the discovered endpoints to a successful probe.

    Args:
        offline: When True, skip every network check.
        server: When set, restrict to that one server.
        list_servers: When True, list servers and exit.
        well_known: When True, run only the discovery probe.
        secret: When True, run only the secret-resolution check.
        summary: When True, run only the configuration summary.
        verbose: When True, show discovered endpoints on a successful probe.
        config: Explicit config path; defaults to the cascade location.
    """
    _doctor.run(
        offline=offline,
        config=config,
        server=server,
        list_servers=list_servers,
        well_known=well_known,
        secret=secret,
        summary=summary,
        verbose=verbose,
    )


@app.command(name="install-kernel-config")
def install_kernel_config(
    target: Path = typer.Option(
        Path("/etc/ipython"),
        "--target",
        help="IPython config directory to write into (default: /etc/ipython).",
    ),
) -> None:
    """Deploy the IPython config that auto-loads the tessera kernel client.

    Writes ``<target>/ipython_config.py`` so notebooks load the tessera
    kernel extension without a manual ``%load_ext``. Refuses to overwrite an
    existing file, and writes nothing else.

    Args:
        target: The IPython config directory to write the config into.
    """
    _install_kernel_config.run(target=target)
