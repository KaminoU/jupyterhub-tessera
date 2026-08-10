"""Metadata and constants for the tessera package.

This module is intentionally import-light: it declares only plain string
constants so the build backend can read ``__version__`` without importing
heavy dependencies, and so ``tessera.meta`` stays cheap to import at runtime.
"""

from __future__ import annotations

__app_name__ = "tessera"
# The import/brand name and the PyPI distribution name differ: the package
# imports as ``tessera`` but ships as ``jupyterhub-tessera`` on PyPI. Use
# ``__dist_name__`` for installed-metadata lookups (importlib.metadata).
__dist_name__ = "jupyterhub-tessera"
__version__ = "1.0.0"
__description__ = (
    "JupyterHub/JupyterLab plugin to acquire and securely store OAuth2/OIDC "
    "tokens, with a per-server status button."
)
__author__ = "Michel TRUONG"
__url__ = "https://github.com/KaminoU/jupyterhub-tessera"
__keywords__ = [
    "jupyter",
    "jupyterlab",
    "jupyterhub",
    "oauth2",
    "oidc",
    "token",
    "refresh-token",
    "access-token",
    "sso",
    "pkce",
    "token-storage",
    "jupyterhub-service",
]
