"""The declared-dependency check refuses an import no declared dependency provides.

The fabricated cases below never look at what happens to be installed: an
environment-dependent test would be flaky from one machine to the next, which
would be a poor showing for the very check that exists to tell a development
environment apart from a clean installation. Only two tests read the real
environment, and both are explicit about it.
"""

from __future__ import annotations

import doctest
from collections.abc import Mapping
from pathlib import Path

import pytest

import check_declared_deps
from check_declared_deps import (
    Finding,
    build_root_index,
    canonical,
    collect_environment,
    collect_import_roots,
    declared_distributions,
    find_undeclared,
    format_finding,
    infer_roots,
    main,
    requirement_name,
    third_party_roots,
)


def write_module(directory: Path, name: str, source: str) -> Path:
    """Write a module into a fabricated package directory."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(source, encoding="utf-8")
    return path


def write_pyproject(directory: Path, dependencies: list[str]) -> Path:
    """Write a minimal pyproject declaring the given dependencies."""
    lines = ["[project]", 'name = "fabricated"', "dependencies = ["]
    lines += [f'    "{dep}",' for dep in dependencies]
    lines += ["]", ""]
    path = directory / "pyproject.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# --- Import collection: ast, never a regex ---------------------------------


def test_imports_are_collected_from_the_syntax_tree_not_from_prose(tmp_path: Path) -> None:
    """Prose that reads like an import is ignored, a nested import is not."""
    write_module(
        tmp_path,
        "prose.py",
        '"""Summary.\n\n'
        "This paragraph explains that the value comes from a provider, and\n"
        "that the caller reads it from the surrounding context.\n"
        '"""\n'
        "\n"
        "\n"
        "def late() -> None:\n"
        "    import httpx\n"
        "\n"
        "    del httpx\n",
    )

    roots = collect_import_roots(tmp_path)

    assert "httpx" in roots
    assert "a" not in roots
    assert "the" not in roots


def test_relative_imports_declare_nothing(tmp_path: Path) -> None:
    """A relative import stays inside the package and yields no root."""
    write_module(tmp_path, "local.py", "from . import sibling\nfrom .deep import thing\n")

    assert collect_import_roots(tmp_path) == {}


def test_dotted_imports_are_reduced_to_their_root(tmp_path: Path) -> None:
    """Only the first segment of a dotted import identifies a distribution."""
    write_module(tmp_path, "deep.py", "import kstlib.auth.oidc\nfrom rich.table import Table\n")

    assert set(collect_import_roots(tmp_path)) == {"kstlib", "rich"}


def test_every_importing_file_is_reported_for_a_root(tmp_path: Path) -> None:
    """A root imported twice names both files, once each."""
    write_module(tmp_path, "one.py", "import httpx\nimport httpx\n")
    write_module(tmp_path, "two.py", "import httpx\n")

    files = collect_import_roots(tmp_path)["httpx"]

    assert len(files) == 2
    assert all(name in " ".join(files) for name in ("one.py", "two.py"))


def test_stdlib_future_and_own_package_are_filtered(tmp_path: Path) -> None:
    """Only third-party roots survive the filter."""
    write_module(
        tmp_path,
        "mixed.py",
        "from __future__ import annotations\nimport json\nimport tessera.store\nimport httpx\n",
    )

    assert set(third_party_roots(collect_import_roots(tmp_path), "tessera")) == {"httpx"}


# --- Declared dependencies --------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("httpx>=0.28,<1", "httpx"),
        ("kstlib[db-crypto]>=3.7.1,<4", "kstlib"),
        ("pyyaml>=6,<7", "pyyaml"),
        ("  rich >= 13 ", "rich"),
        ("tomli>=2; python_version < '3.11'", "tomli"),
        ("types-PyYAML", "types-PyYAML"),
    ],
)
def test_requirement_name_strips_extras_specifiers_and_markers(spec: str, expected: str) -> None:
    """The distribution name is read without its extras, specifiers, or markers."""
    assert requirement_name(spec) == expected


def test_requirement_name_rejects_a_spec_it_cannot_read() -> None:
    """An unreadable requirement is an error, never a silently skipped entry."""
    with pytest.raises(ValueError, match="distribution name"):
        requirement_name(">=1.0")


def test_declared_distributions_are_canonicalized(tmp_path: Path) -> None:
    """Declared names are compared in their PEP 503 canonical form."""
    pyproject = write_pyproject(tmp_path, ["types-PyYAML>=6,<7", "kstlib[db-crypto]>=3.7.1,<4"])

    assert declared_distributions(pyproject) == {"types-pyyaml", "kstlib"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("PyYAML", "pyyaml"), ("types-PyYAML", "types-pyyaml"), ("zope.interface", "zope-interface")],
)
def test_canonical_folds_case_and_separators(raw: str, expected: str) -> None:
    """Case and runs of separators fold to a single canonical spelling."""
    assert canonical(raw) == expected


# --- Root index: stdlib mapping, then inference -----------------------------


def test_inference_recovers_a_root_the_mapping_misses() -> None:
    """A distribution without top_level.txt still resolves, through its files."""
    index = build_root_index({}, {"Fabricated-Lib": ["fablib/__init__.py", "fablib/core.py"]})

    assert index["fablib"] == {"fabricated-lib"}


def test_the_mapping_wins_over_the_inference() -> None:
    """The fallback only adds; it never contradicts what the mapping resolved."""
    index = build_root_index(
        {"fablib": ["Fabricated-Lib"]},
        {"Impostor": ["fablib/__init__.py"]},
    )

    assert index["fablib"] == {"fabricated-lib"}


def test_two_distributions_can_provide_the_same_inferred_root() -> None:
    """A namespace shared by two distributions keeps both providers."""
    index = build_root_index(
        {},
        {"First-Half": ["shared/one.py"], "Second-Half": ["shared/two.py"]},
    )

    assert index["shared"] == {"first-half", "second-half"}


def test_a_single_module_distribution_yields_its_module_name() -> None:
    """A distribution shipping one flat module resolves to that module."""
    index = build_root_index({}, {"Six-Like": ["sixlike.py"]})

    assert index["sixlike"] == {"six-like"}


@pytest.mark.parametrize(
    "entry",
    [
        "../../Scripts/tool.exe",
        "fabricated-1.0.dist-info/RECORD",
        "fabricated-1.0.egg-info/PKG-INFO",
        "fabricated-1.0.data/scripts/tool",
        "not-an-identifier/mod.py",
        "9leading/mod.py",
        "README.md",
    ],
)
def test_inference_discards_entries_that_are_not_import_roots(entry: str) -> None:
    """Script paths, metadata directories, and non-identifiers are discarded."""
    assert infer_roots([entry]) == set()


def test_inference_accepts_windows_separators() -> None:
    """A RECORD written with backslashes still yields its root."""
    assert infer_roots(["fablib\\core.py"]) == {"fablib"}


def test_inference_normalizes_a_leading_current_directory() -> None:
    """A leading ./ is not an escape, it points at the same root."""
    assert infer_roots(["./fablib/core.py"]) == {"fablib"}


# --- The comparison itself --------------------------------------------------


def test_a_root_matching_a_declared_name_passes_without_the_index() -> None:
    """Step one settles the common case, so an empty index changes nothing."""
    assert find_undeclared({"httpx": ("mod.py",)}, {"httpx"}, {}) == []


def test_a_root_resolved_through_the_mapping_passes() -> None:
    """The PyYAML case: the import root differs from the declared name."""
    findings = find_undeclared({"yaml": ("mod.py",)}, {"pyyaml"}, {"yaml": {"pyyaml"}})

    assert findings == []


def test_a_root_resolved_through_the_inference_passes() -> None:
    """Same case, resolved by the fallback instead of the mapping."""
    index = build_root_index({}, {"PyYAML": ["yaml/__init__.py"]})

    assert find_undeclared({"yaml": ("mod.py",)}, {"pyyaml"}, index) == []


def test_an_undeclared_root_is_reported_with_its_provider() -> None:
    """A provider that exists but is not declared names the distribution."""
    findings = find_undeclared({"yaml": ("mod.py",)}, {"httpx"}, {"yaml": {"pyyaml"}})

    assert findings == [Finding(root="yaml", providers=("pyyaml",), files=("mod.py",))]


def test_an_unresolvable_root_is_reported_with_no_provider() -> None:
    """No installed distribution provides the root: still a single failure."""
    findings = find_undeclared({"ghost": ("mod.py",)}, {"httpx"}, {})

    assert findings == [Finding(root="ghost", providers=(), files=("mod.py",))]


def test_findings_name_the_provider_and_the_importing_file() -> None:
    """The message carries what a reader needs to act, and no more."""
    message = format_finding(Finding(root="yaml", providers=("pyyaml",), files=("mod.py",)))

    assert "'yaml'" in message
    assert "'pyyaml'" in message
    assert "mod.py" in message


def test_an_unresolvable_finding_says_the_environment_may_be_incomplete() -> None:
    """The message distinguishes a missing declaration from a bare environment."""
    message = format_finding(Finding(root="ghost", providers=(), files=("mod.py",)))

    assert "ghost" in message
    assert "environment" in message


# --- Entry point ------------------------------------------------------------


def test_main_accepts_a_tree_whose_imports_are_all_declared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A package importing only the stdlib needs no declaration at all."""
    package = tmp_path / "pkg"
    write_module(package, "clean.py", "from __future__ import annotations\nimport json\n")
    pyproject = write_pyproject(tmp_path, [])

    assert main(["--package-dir", str(package), "--pyproject", str(pyproject)]) == 0
    assert "all declared" in capsys.readouterr().out


def test_main_refuses_a_tree_importing_an_undeclared_module(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The undeclared root is named on stderr and the exit code is non zero."""
    package = tmp_path / "pkg"
    write_module(package, "dirty.py", "import tessera_ghost_module\n")
    pyproject = write_pyproject(tmp_path, [])

    assert main(["--package-dir", str(package), "--pyproject", str(pyproject)]) == 1
    captured = capsys.readouterr()
    assert "tessera_ghost_module" in captured.err
    assert "REFUSED" in captured.err


def test_main_ignores_the_package_it_is_checking(tmp_path: Path) -> None:
    """A package importing itself by name declares nothing."""
    package = tmp_path / "tessera"
    write_module(package, "self.py", "import tessera.store\n")
    pyproject = write_pyproject(tmp_path, [])

    assert main(["--package-dir", str(package), "--pyproject", str(pyproject)]) == 0


# --- The two tests that read the real environment ---------------------------


def test_collect_environment_returns_a_mapping_and_the_declared_files() -> None:
    """Shape only: asserting on installed content would be machine dependent."""
    mapping, dist_files = collect_environment()

    assert isinstance(mapping, Mapping)
    assert isinstance(dist_files, dict)
    assert all(isinstance(files, list) for files in dist_files.values())


def test_the_repository_declares_every_module_it_imports() -> None:
    """The regression this whole check exists for, run against the real tree."""
    assert main([]) == 0


def test_the_docstring_examples_are_accurate() -> None:
    """The doctest job only walks src/tessera, so the examples run from here."""
    assert doctest.testmod(check_declared_deps).failed == 0
