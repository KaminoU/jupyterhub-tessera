"""Import every module of the installed package, on its runtime dependencies alone.

Declaring a dependency is one half of the contract; the other half is that the
package actually imports once only the declared ones are installed. Nothing
else in this repository can see that half. Every CI job and every tox test
environment installs the dev extra, so they all reproduce a graph far wider
than what a user receives, and the CI installs from the lock on top of that,
so it never even resolves afresh.

The module list is discovered from the INSTALLED package, never from the
source tree and never from a list written by hand: a hand written list ages
badly, and reading the source tree would test the very thing that is not being
shipped. Discovery walks the package tree, imports each module, and resolves
the names each level re-exports through ``__all__``, since a name that is
listed but not reachable is the same defect one indirection further.

The first failure stops the walk. Once a package fails to import, everything
below it fails for the same reason, and the first one is the actionable one.

Run it against an installation, not against a checkout::

    python scripts/check_clean_install.py
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PACKAGE = "tessera"
DEFAULT_SOURCE_ROOT = REPO_ROOT / "src"


@dataclass(frozen=True)
class Failure:
    """A module that could not be imported, or a name it fails to re-export.

    Attributes:
        module: Fully qualified name of the offending module.
        reason: What went wrong, phrased for someone reading a CI log.
    """

    module: str
    reason: str


def describe(error: BaseException) -> str:
    """Render an exception as its type and message, without a traceback."""
    return f"{type(error).__name__}: {error}"


def module_location(module: ModuleType) -> Path | None:
    """Return the directory a module was loaded from, when it has one."""
    file_name = getattr(module, "__file__", None)
    if file_name is not None:
        return Path(file_name).resolve().parent
    paths = list(getattr(module, "__path__", ()))
    return Path(paths[0]).resolve() if paths else None


def is_within(path: Path, root: Path) -> bool:
    """Report whether a path sits inside a directory."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def child_module_names(module: ModuleType) -> list[str]:
    """Return the immediate submodule names of a package, empty for a module."""
    paths = list(getattr(module, "__path__", ()))
    if not paths:
        return []
    return sorted(f"{module.__name__}.{info.name}" for info in pkgutil.iter_modules(paths))


def missing_exports(module: ModuleType) -> list[str]:
    """Return the ``__all__`` entries the module does not actually provide."""
    declared: Sequence[str] = getattr(module, "__all__", ())
    return [name for name in declared if not hasattr(module, name)]


def check_tree(package: ModuleType) -> Failure | None:
    """Import every module below a package and resolve what each re-exports.

    Args:
        package: The already imported root of the tree.

    Returns:
        The first failure met, or ``None`` when the whole tree imports.
    """
    pending = [package]
    while pending:
        module = pending.pop(0)
        missing = missing_exports(module)
        if missing:
            listed = ", ".join(missing)
            return Failure(module.__name__, f"__all__ lists names it does not provide: {listed}")
        for name in child_module_names(module):
            try:
                pending.append(importlib.import_module(name))
            # Any exception at import time is a failure, not only ImportError.
            except Exception as error:
                return Failure(name, describe(error))
    return None


def check_package(name: str, source_root: Path) -> Failure | None:
    """Import a package from its installation and walk it.

    Args:
        name: Name of the package to import.
        source_root: Directory the package must NOT be imported from, so a
            checkout on the path cannot stand in for an installation. The
            guard has no off switch: without it the check reads the very
            thing that is not being shipped.

    Returns:
        The first failure met, or ``None`` when everything imports.
    """
    try:
        package = importlib.import_module(name)
    # Any exception at import time is a failure, not only ImportError.
    except Exception as error:
        return Failure(name, describe(error))
    located = module_location(package)
    if located is not None and is_within(located, source_root):
        return Failure(
            name,
            f"imported from the source tree at {located}, not from an installation; "
            "this check is meaningless unless it reads what gets shipped",
        )
    return check_tree(package)


def imported_count(name: str) -> int:
    """Count the modules of a package that are present in the import cache.

    Read after a successful walk, this is what was actually imported, so it
    needs no second traversal of the tree.
    """
    prefix = f"{name}."
    return 1 + sum(1 for key in sys.modules if key.startswith(prefix))


def main(argv: Sequence[str] | None = None) -> int:
    """Import every module of an installed package and report the first failure.

    Args:
        argv: Command line arguments, defaulting to those of the process.

    Returns:
        ``0`` when the whole package imports, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(description="Import an installed package end to end.")
    parser.add_argument("--package", default=DEFAULT_PACKAGE)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    args = parser.parse_args(argv)

    source_root = Path(args.source_root).resolve()
    failure = check_package(args.package, source_root)
    if failure is not None:
        print(
            "clean-install: REFUSED, the installed package does not import "
            "on its declared dependencies alone",
            file=sys.stderr,
        )
        print(f"    {failure.module}: {failure.reason}", file=sys.stderr)
        print(
            "    fix: declare the missing dependency, or drop the import",
            file=sys.stderr,
        )
        return 1

    package = importlib.import_module(args.package)
    total = imported_count(args.package)
    print(f"clean-install: {total} modules imported from {module_location(package)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
