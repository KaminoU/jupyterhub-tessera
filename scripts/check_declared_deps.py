"""Refuse an import that no declared dependency provides.

A module imported by the package but absent from ``[project].dependencies``
keeps working for exactly as long as some other dependency happens to pull it
in. The day that transitive edge moves, the package breaks at import time for
everyone who installed it from a registry, and no development environment ever
notices: installing the dev extra reproduces the same transitive graph the
sources were written against.

This check compares what the sources import against what the project declares.
Import roots are read from the syntax tree rather than matched textually, so
prose that reads like an import is not mistaken for one, and an import nested
in a function is not missed.

Resolving a root to a distribution is a two step affair, because the two names
need not match (``yaml`` is shipped by ``PyYAML``). The standard mapping is
consulted first; where it returns nothing, the root is inferred from the files
each distribution declares. The fallback matters on the supported floor, whose
``packages_distributions`` only knows the distributions that ship a
``top_level.txt`` and infers nothing for the others. It only ever adds a
resolution, never replaces one, so it cannot disagree with the standard answer.

Run it from the repository root::

    python scripts/check_declared_deps.py
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import distributions, packages_distributions
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PACKAGE_DIR = REPO_ROOT / "src" / "tessera"
DEFAULT_PYPROJECT = REPO_ROOT / "pyproject.toml"

# PEP 503 canonicalization: fold case and collapse runs of separators.
_SEPARATORS = re.compile(r"[-_.]+")

# The leading name of a PEP 508 requirement, before any extra, specifier, or
# marker. A regular expression is the right tool here (this is a flat string
# grammar, and the standard library ships no requirement parser); the ban on
# textual matching applies to reading import statements, which is done with ast.
_REQUIREMENT_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

# Directory suffixes a wheel installs alongside the importable packages.
_NOT_PACKAGES = (".dist-info", ".egg-info", ".data")


@dataclass(frozen=True)
class Finding:
    """An import root that no declared dependency provides.

    Attributes:
        root: The top-level name that is imported.
        providers: Canonical names of the installed distributions providing it,
            empty when none was found.
        files: The files importing it, for display.
    """

    root: str
    providers: tuple[str, ...]
    files: tuple[str, ...]


def canonical(name: str) -> str:
    """Return the PEP 503 canonical form of a distribution name.

    Args:
        name: A distribution name in any spelling.

    Returns:
        The canonical spelling, lowercase with separators collapsed.

    Examples:
        >>> canonical("types-PyYAML")
        'types-pyyaml'
    """
    return _SEPARATORS.sub("-", name).lower()


def requirement_name(spec: str) -> str:
    """Read the distribution name out of a requirement specification.

    Args:
        spec: A PEP 508 requirement, with optional extras, specifiers, marker.

    Returns:
        The distribution name alone.

    Raises:
        ValueError: If no name can be read from the specification.

    Examples:
        >>> requirement_name("kstlib[db-crypto]>=3.6.2,<4")
        'kstlib'
    """
    match = _REQUIREMENT_NAME.match(spec)
    if match is None:
        raise ValueError(f"cannot read a distribution name from {spec!r}")
    return match.group(1)


def display_path(path: Path) -> str:
    """Render a path relative to the working directory when it sits below it."""
    try:
        return path.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return path.as_posix()


def collect_import_roots(package_dir: Path) -> dict[str, tuple[str, ...]]:
    """Map every imported top-level name to the files importing it.

    Args:
        package_dir: Directory whose ``.py`` files are parsed, recursively.

    Returns:
        Each top-level import name, mapped to the files importing it. Relative
        imports contribute nothing: they resolve inside the package.
    """
    found: dict[str, list[str]] = {}
    for path in sorted(package_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for name in _imported_names(node):
                found.setdefault(name.split(".")[0], []).append(display_path(path))
    return {root: tuple(dict.fromkeys(files)) for root, files in sorted(found.items())}


def _imported_names(node: ast.AST) -> list[str]:
    """Return the module names a single syntax node imports."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return [node.module]
    return []


def third_party_roots(
    roots: Mapping[str, tuple[str, ...]], own_package: str
) -> dict[str, tuple[str, ...]]:
    """Drop the roots that no dependency can ever provide.

    Args:
        roots: Import roots mapped to the files importing them.
        own_package: Name of the package being checked, which provides itself.

    Returns:
        The roots that a declared dependency is expected to provide.
    """
    ignored = set(sys.stdlib_module_names) | {"__future__", own_package}
    return {root: files for root, files in roots.items() if root not in ignored}


def declared_distributions(pyproject: Path) -> set[str]:
    """Return the canonical names declared in ``[project].dependencies``.

    Args:
        pyproject: Path to the project file to read.

    Returns:
        The canonical name of every declared runtime dependency.
    """
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    declared: list[str] = data["project"]["dependencies"]
    return {canonical(requirement_name(spec)) for spec in declared}


def infer_roots(files: Iterable[str]) -> set[str]:
    """Infer the import roots a distribution installs, from the files it declares.

    Args:
        files: Paths a distribution declares, as recorded at install time.

    Returns:
        The top-level importable names among them. Entries that cannot be one
        are discarded: paths escaping the installation directory, metadata and
        data directories, and anything that is not a valid identifier.
    """
    roots: set[str] = set()
    for raw in files:
        parts = Path(raw.replace("\\", "/")).parts
        if not parts or parts[0] == ".." or parts[0].endswith(_NOT_PACKAGES):
            continue
        if len(parts) > 1:
            candidate = parts[0]
        elif parts[0].endswith(".py"):
            candidate = parts[0][:-3]
        else:
            continue
        if candidate.isidentifier():
            roots.add(candidate)
    return roots


def build_root_index(
    mapping: Mapping[str, list[str]], dist_files: Mapping[str, list[str]]
) -> dict[str, set[str]]:
    """Map every import root to the distributions providing it.

    Args:
        mapping: The standard import root to distribution names mapping.
        dist_files: Each installed distribution, mapped to the files it declares.

    Returns:
        Import roots mapped to canonical distribution names. The inference runs
        only for roots the standard mapping says nothing about, so it can add a
        resolution but never replace one.
    """
    index = {root: {canonical(name) for name in names} for root, names in mapping.items()}
    inferred: dict[str, set[str]] = {}
    for name, files in dist_files.items():
        for root in infer_roots(files):
            if root not in index:
                inferred.setdefault(root, set()).add(canonical(name))
    index.update(inferred)
    return index


def collect_environment() -> tuple[Mapping[str, list[str]], dict[str, list[str]]]:
    """Read the installed environment.

    Returns:
        The standard import root mapping, and every installed distribution
        mapped to the files it declares.
    """
    dist_files: dict[str, list[str]] = {}
    for dist in distributions():
        recorded = dist.files or ()
        dist_files.setdefault(dist.name, []).extend(str(entry) for entry in recorded)
    return packages_distributions(), dist_files


def find_undeclared(
    roots: Mapping[str, tuple[str, ...]],
    declared: set[str],
    index: Mapping[str, set[str]],
) -> list[Finding]:
    """Return the import roots no declared dependency provides.

    Args:
        roots: Third-party import roots mapped to the files importing them.
        declared: Canonical names of the declared dependencies.
        index: Import roots mapped to the distributions providing them.

    Returns:
        One finding per unsatisfied root, in the order the roots were given.
    """
    findings: list[Finding] = []
    for root, files in roots.items():
        if canonical(root) in declared:
            continue
        providers = index.get(root, set())
        if providers & declared:
            continue
        findings.append(Finding(root=root, providers=tuple(sorted(providers)), files=files))
    return findings


def format_finding(finding: Finding) -> str:
    """Render a finding as the line a reader can act on."""
    if finding.providers:
        provided_by = ", ".join(repr(name) for name in finding.providers)
        head = (
            f"{finding.root!r} is provided by {provided_by}, "
            "which [project].dependencies does not declare"
        )
    else:
        head = (
            f"{finding.root!r} is imported and no installed distribution provides it "
            "(undeclared, or this environment is incomplete)"
        )
    return head + "".join(f"\n      imported by {name}" for name in finding.files)


def main(argv: Sequence[str] | None = None) -> int:
    """Compare the imported roots against the declared dependencies.

    Args:
        argv: Command line arguments, defaulting to those of the process.

    Returns:
        ``0`` when every third-party import is declared, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(description="Refuse an undeclared import.")
    parser.add_argument("--package-dir", type=Path, default=DEFAULT_PACKAGE_DIR)
    parser.add_argument("--pyproject", type=Path, default=DEFAULT_PYPROJECT)
    args = parser.parse_args(argv)

    package_dir: Path = args.package_dir
    roots = third_party_roots(collect_import_roots(package_dir), package_dir.name)
    declared = declared_distributions(args.pyproject)
    mapping, dist_files = collect_environment()
    findings = find_undeclared(roots, declared, build_root_index(mapping, dist_files))

    if not findings:
        print(f"declared-deps: {len(roots)} third-party import roots, all declared")
        return 0

    print(
        "declared-deps: REFUSED, an imported module is not a declared dependency",
        file=sys.stderr,
    )
    for finding in findings:
        print(f"    {format_finding(finding)}", file=sys.stderr)
    print("    fix: declare it in [project].dependencies, or drop the import", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
