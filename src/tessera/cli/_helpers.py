"""Shared helpers for the tessera CLI commands.

Hosts the small pieces reused across the command modules so each command
keeps a single implementation of the common failure path.
"""

from __future__ import annotations

from typing import NoReturn

import typer
from kstlib.ui import PanelManager

_panels = PanelManager()


def _fail(message: str) -> NoReturn:
    """Show an actionable error panel and exit non-zero."""
    _panels.print_panel("error", payload=message)
    raise typer.Exit(1)
