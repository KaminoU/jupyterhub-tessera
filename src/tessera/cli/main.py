"""Console-script entry point for the tessera CLI (``tessera.cli.main:main``)."""

from __future__ import annotations

from tessera.cli.app import app


def main() -> None:
    """Run the tessera Typer application."""
    app()


if __name__ == "__main__":
    main()
