"""Internal helpers shared across tessera sub-packages.

Nothing in this package is part of tessera's public API. Modules live here
so sibling packages (the token store, the OAuth flow) can share a single
implementation instead of duplicating it.
"""

from __future__ import annotations
