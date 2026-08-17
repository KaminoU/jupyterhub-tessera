"""The clean-install check imports an installed package end to end.

Every package here is fabricated under a temporary root and removed from the
import cache afterwards. Nothing asserts on what happens to be installed: a
test that depended on the ambient environment would be measuring the very
thing this check exists to distinguish from a real installation.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest

from check_clean_install import (
    Failure,
    check_package,
    check_tree,
    child_module_names,
    describe,
    imported_count,
    is_within,
    missing_exports,
    module_location,
)
from check_clean_install import (
    main as clean_install_main,
)

PackageBuilder = Callable[[str, dict[str, str]], Path]

SOUND = {
    "__init__.py": 'from .core import value\n\n__all__ = ["value"]\n',
    "core.py": "value = 1\n",
    "sub/__init__.py": "",
    "sub/leaf.py": "leaf = True\n",
}

# The source root guard can no longer be switched off, so every call has to name
# a root. Every fabricated package here is built under the pytest temporary
# directory, so this real one keeps the guard armed without ever firing on one.
ELSEWHERE = Path(__file__).resolve().parent


@pytest.fixture
def build_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[PackageBuilder]:
    """Build importable packages under a temporary root, then forget them."""
    monkeypatch.syspath_prepend(str(tmp_path))
    created: list[str] = []

    def build(name: str, modules: dict[str, str]) -> Path:
        root = tmp_path / name
        for relative, source in modules.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        created.append(name)
        importlib.invalidate_caches()
        return root

    yield build

    for name in created:
        for key in [k for k in sys.modules if k == name or k.startswith(f"{name}.")]:
            del sys.modules[key]


# --- The sound case ---------------------------------------------------------


def test_a_sound_package_imports_end_to_end(build_package: PackageBuilder) -> None:
    """Every module imports and every re-exported name resolves."""
    build_package("cleanpkg_sound", SOUND)

    assert check_package("cleanpkg_sound", ELSEWHERE) is None


def test_every_module_of_the_tree_is_actually_imported(build_package: PackageBuilder) -> None:
    """The walk reaches the submodules, not only the package roots."""
    build_package("cleanpkg_counted", SOUND)

    assert check_package("cleanpkg_counted", ELSEWHERE) is None
    assert imported_count("cleanpkg_counted") == 4


# --- Import failures --------------------------------------------------------


def test_a_submodule_that_cannot_import_is_reported(build_package: PackageBuilder) -> None:
    """The failing submodule is named, with the exception that stopped it."""
    build_package(
        "cleanpkg_broken",
        {"__init__.py": "", "phantom.py": "import cleanpkg_absent_dependency\n"},
    )

    failure = check_package("cleanpkg_broken", ELSEWHERE)

    assert failure is not None
    assert failure.module == "cleanpkg_broken.phantom"
    assert "ModuleNotFoundError" in failure.reason


def test_a_failure_nested_three_levels_deep_is_reached(build_package: PackageBuilder) -> None:
    """Recursion is real: the walk does not stop at the first level."""
    build_package(
        "cleanpkg_deep",
        {
            "__init__.py": "",
            "one/__init__.py": "",
            "one/two/__init__.py": "",
            "one/two/broken.py": "import cleanpkg_absent_dependency\n",
        },
    )

    failure = check_package("cleanpkg_deep", ELSEWHERE)

    assert failure is not None
    assert failure.module == "cleanpkg_deep.one.two.broken"


def test_a_failure_that_is_not_an_import_error_still_fails(build_package: PackageBuilder) -> None:
    """Any exception raised at import time counts, not only ImportError."""
    build_package(
        "cleanpkg_raises",
        {"__init__.py": "", "angry.py": "raise RuntimeError('module said no')\n"},
    )

    failure = check_package("cleanpkg_raises", ELSEWHERE)

    assert failure is not None
    assert failure.module == "cleanpkg_raises.angry"
    assert "RuntimeError" in failure.reason


def test_the_walk_stops_at_the_first_failure(build_package: PackageBuilder) -> None:
    """Two broken modules, one report, and the order is deterministic."""
    build_package(
        "cleanpkg_two_faults",
        {
            "__init__.py": "",
            "a_broken.py": "import cleanpkg_absent_dependency\n",
            "z_broken.py": "import cleanpkg_absent_dependency\n",
        },
    )

    failure = check_package("cleanpkg_two_faults", ELSEWHERE)

    assert failure is not None
    assert failure.module == "cleanpkg_two_faults.a_broken"


def test_a_package_that_is_not_installed_at_all_is_reported() -> None:
    """The root itself failing to import is a failure like any other."""
    failure = check_package("cleanpkg_never_created", ELSEWHERE)

    assert failure is not None
    assert failure.module == "cleanpkg_never_created"
    assert "ModuleNotFoundError" in failure.reason


# --- Re-exports -------------------------------------------------------------


def test_a_name_listed_in_all_but_absent_is_reported(build_package: PackageBuilder) -> None:
    """A name that is advertised but unreachable is the same defect, indirect."""
    build_package("cleanpkg_liar", {"__init__.py": '__all__ = ["absent_name"]\n'})

    failure = check_package("cleanpkg_liar", ELSEWHERE)

    assert failure is not None
    assert failure.module == "cleanpkg_liar"
    assert "absent_name" in failure.reason


def test_a_missing_re_export_is_caught_below_the_root(build_package: PackageBuilder) -> None:
    """__all__ is resolved at each level, not only on the package root."""
    build_package(
        "cleanpkg_deep_liar",
        {"__init__.py": "", "sub/__init__.py": '__all__ = ["absent_name"]\n'},
    )

    failure = check_package("cleanpkg_deep_liar", ELSEWHERE)

    assert failure is not None
    assert failure.module == "cleanpkg_deep_liar.sub"


def test_a_module_without_all_declares_nothing(build_package: PackageBuilder) -> None:
    """No __all__ means no promise to check."""
    build_package("cleanpkg_silent", {"__init__.py": "x = 1\n"})
    module = importlib.import_module("cleanpkg_silent")

    assert missing_exports(module) == []


# --- The source tree guard --------------------------------------------------


def test_a_package_imported_from_the_source_root_is_refused(
    build_package: PackageBuilder, tmp_path: Path
) -> None:
    """A checkout on the path must not stand in for an installation."""
    build_package("cleanpkg_from_source", SOUND)

    failure = check_package("cleanpkg_from_source", source_root=tmp_path)

    assert failure is not None
    assert "source tree" in failure.reason


def test_the_source_root_guard_passes_when_the_package_sits_elsewhere(
    build_package: PackageBuilder, tmp_path: Path
) -> None:
    """The guard only fires for a package under the named directory."""
    build_package("cleanpkg_elsewhere", SOUND)

    assert check_package("cleanpkg_elsewhere", source_root=tmp_path / "somewhere-else") is None


# --- Small pieces -----------------------------------------------------------


def test_child_module_names_is_empty_for_a_plain_module(build_package: PackageBuilder) -> None:
    """A module has no children, only a package does."""
    build_package("cleanpkg_flat", {"__init__.py": "", "solo.py": "x = 1\n"})
    module = importlib.import_module("cleanpkg_flat.solo")

    assert child_module_names(module) == []


def test_child_module_names_are_sorted(build_package: PackageBuilder) -> None:
    """A stable order is what makes the first reported failure reproducible."""
    build_package(
        "cleanpkg_ordered",
        {"__init__.py": "", "zulu.py": "", "alpha.py": "", "mike.py": ""},
    )
    package = importlib.import_module("cleanpkg_ordered")

    assert child_module_names(package) == [
        "cleanpkg_ordered.alpha",
        "cleanpkg_ordered.mike",
        "cleanpkg_ordered.zulu",
    ]


def test_check_tree_accepts_an_already_imported_package(build_package: PackageBuilder) -> None:
    """The tree walk works on a module object, independently of resolution."""
    build_package("cleanpkg_direct", SOUND)
    package = importlib.import_module("cleanpkg_direct")

    assert check_tree(package) is None


def test_module_location_points_at_the_directory(build_package: PackageBuilder) -> None:
    """The reported location is where the package was loaded from."""
    root = build_package("cleanpkg_located", SOUND)
    package = importlib.import_module("cleanpkg_located")

    assert module_location(package) == root.resolve()


def test_module_location_falls_back_to_the_search_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A namespace package carries no __file__, only a search path."""
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "cleanpkg_namespace").mkdir()
    (tmp_path / "cleanpkg_namespace" / "leaf.py").write_text("x = 1\n", encoding="utf-8")
    importlib.invalidate_caches()
    package = importlib.import_module("cleanpkg_namespace")
    try:
        assert package.__file__ is None
        assert module_location(package) == (tmp_path / "cleanpkg_namespace").resolve()
    finally:
        for key in [k for k in sys.modules if k.startswith("cleanpkg_namespace")]:
            del sys.modules[key]


def test_module_location_is_unknown_without_a_file_or_a_path() -> None:
    """A module built in memory was loaded from nowhere, and says so."""
    assert module_location(ModuleType("cleanpkg_in_memory")) is None


@pytest.mark.parametrize(
    ("path", "root", "expected"),
    [
        (Path("/a/b/c"), Path("/a"), True),
        (Path("/a/b/c"), Path("/a/b/c"), True),
        (Path("/a/b"), Path("/a/b/c"), False),
        (Path("/x/y"), Path("/a"), False),
    ],
)
def test_is_within(path: Path, root: Path, expected: bool) -> None:
    """Containment is decided by path arithmetic, never by string prefix."""
    assert is_within(path, root) is expected


def test_describe_renders_the_type_and_the_message() -> None:
    """A CI log needs the kind of failure and its text, not a traceback."""
    assert describe(ValueError("bad value")) == "ValueError: bad value"


# --- Entry point ------------------------------------------------------------


def test_main_reports_the_module_count_on_success(
    build_package: PackageBuilder, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pass says what it covered, so an empty walk cannot look like a pass."""
    build_package("cleanpkg_main_ok", SOUND)

    assert clean_install_main(["--package", "cleanpkg_main_ok"]) == 0
    assert "4 modules imported" in capsys.readouterr().out


def test_main_refuses_and_names_the_module(
    build_package: PackageBuilder, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failing module and the fix land on stderr, exit code is non zero."""
    build_package(
        "cleanpkg_main_ko",
        {"__init__.py": "", "phantom.py": "import cleanpkg_absent_dependency\n"},
    )

    assert clean_install_main(["--package", "cleanpkg_main_ko"]) == 1
    captured = capsys.readouterr()
    assert "REFUSED" in captured.err
    assert "cleanpkg_main_ko.phantom" in captured.err
    assert "declare the missing dependency" in captured.err


def test_main_refuses_a_package_under_the_declared_source_root(
    build_package: PackageBuilder, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The guard is wired into the entry point, not only into the function."""
    build_package("cleanpkg_main_source", SOUND)

    code = clean_install_main(["--package", "cleanpkg_main_source", "--source-root", str(tmp_path)])

    assert code == 1
    assert "source tree" in capsys.readouterr().err


def test_main_arms_the_guard_with_the_default_source_root(
    build_package: PackageBuilder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The form production calls, with no flag at all, still refuses a checkout."""
    build_package("cleanpkg_main_default", SOUND)
    monkeypatch.setattr("check_clean_install.DEFAULT_SOURCE_ROOT", tmp_path)

    code = clean_install_main(["--package", "cleanpkg_main_default"])

    assert code == 1
    assert "source tree" in capsys.readouterr().err


def test_a_failure_is_comparable_by_value() -> None:
    """Findings are plain data, which is what makes them assertable."""
    assert Failure("mod", "reason") == Failure("mod", "reason")
