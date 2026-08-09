"""Smoke tests for tessera package metadata and the version source of truth."""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import tessera
from tessera import meta

_REPO_ROOT = Path(__file__).resolve().parent.parent

# A pre-release is spelled 1.0.0rc1 on the Python side (PEP 440) and
# 1.0.0-rc.1 on the npm side (semver). Folding the npm spelling onto the
# Python one is the only way to compare the two sources.
#
# MIRROR: the `canon` shell helper of the `version-check` job in
# .github/workflows/ci.yml does the same folding, in the same order, over
# the same four sources. The two must stay in agreement: a change here
# without the matching change there would let a real drift ship green.
_NPM_TO_PEP440 = (
    ("-alpha.", "a"),
    ("-beta.", "b"),
    ("-rc.", "rc"),
    ("-alpha", "a"),
    ("-beta", "b"),
    ("-rc", "rc"),
)


def _canon(version: str) -> str:
    """Fold an npm pre-release spelling onto its PEP 440 equivalent.

    Args:
        version: A version string in either spelling.

    Returns:
        The version with any npm pre-release segment rewritten PEP 440 style.
        A final version carries no pre-release segment and passes through
        untouched, so exact comparison still applies to it.
    """
    for npm_form, pep440_form in _NPM_TO_PEP440:
        version = version.replace(npm_form, pep440_form)
    return version


def test_package_reexports_version() -> None:
    """The package root re-exports ``__version__`` from ``tessera.meta``."""
    assert tessera.__version__ == meta.__version__


def test_version_matches_package_json() -> None:
    """The Python version matches the npm version, spellings folded.

    This is the local lock between the two sources of truth: the npm side is
    already pinned to ``frontend/src/version.ts`` by a Vitest test, so with
    this one the three declared versions are guarded by tests that run in the
    normal suite, and a drift no longer waits for the release tag to surface.
    """
    package_json = json.loads((_REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    assert meta.__version__ == _canon(package_json["version"])


def test_version_is_pep440() -> None:
    """``__version__`` is a PEP 440 release, optionally a pre-release.

    The pattern refuses an npm spelling written on the Python side by mistake
    (``1.0.0-rc.1``), which the build backend would reject when it reads this
    module as the version source.
    """
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?", meta.__version__) is not None


def test_entry_modules_expose_main() -> None:
    """The ``python -m`` entry modules import and expose ``main``."""
    assert callable(importlib.import_module("tessera.__main__").main)
    assert callable(importlib.import_module("tessera.cli.__main__").main)
