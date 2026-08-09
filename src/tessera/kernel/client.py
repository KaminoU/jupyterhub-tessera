"""Synchronous, kernel-side client for tessera access tokens.

This is what a notebook imports (directly or as an IPython extension) to
obtain a fresh OAuth access token from the tessera JupyterHub service. It
exposes two names:

``get_token("<server>")``
    Return the current access token for a declared server, as a string.

``TESSERA_TOKEN["<server>"]``
    The same value through a lazy mapping, convenient inside a cell.

Everything here is deliberately synchronous: a notebook cell runs in a
synchronous context, and the service already coalesces concurrent
refreshes on its side. The client speaks plain HTTP to the running
service and never imports the async service-side code (orchestrator,
store), so it stays importable in a bare kernel.

The service base URL is resolved from the environment, highest priority
first: ``TESSERA_URL`` when set, otherwise derived from
``JUPYTERHUB_PUBLIC_URL`` and ``JUPYTERHUB_BASE_URL``. The request
authenticates with ``$JUPYTERHUB_API_TOKEN``, the single-user server's
own Hub API token.

Token hygiene: no token value is ever cached, logged, printed, or placed
in an error message. Each access is a fresh request, so a token revoked
at the provider is never served from a stale value.
"""

from __future__ import annotations

import os
from typing import Protocol
from urllib.parse import quote, urlsplit

import httpx

_HTTP_TIMEOUT = 10.0
_ERROR_TEXT_LIMIT = 200
_INJECTED_NAMES = ("TESSERA_TOKEN", "get_token")


class TesseraTokenError(Exception):
    """Raised when a fresh access token cannot be obtained.

    The message is the actionable detail returned by the tessera service
    (or a local diagnosis of a misconfigured environment), never a token
    value nor an ``Authorization`` header.
    """


class _IPythonShell(Protocol):
    """Structural view of the IPython shell the extension hooks use."""

    user_ns: dict[str, object]

    def push(self, variables: dict[str, object]) -> None:
        """Merge ``variables`` into the interactive namespace."""


def _new_client() -> httpx.Client:
    """Build the HTTP client used to reach the service.

    Isolated in one function so a test can inject a mock transport with no
    network round-trip. The timeout is mandatory: a hung service must
    never block a notebook cell indefinitely.
    """
    return httpx.Client(timeout=_HTTP_TIMEOUT)


def _api_token() -> str:
    """Return the single-user server's JupyterHub API token.

    Returns:
        The value of ``$JUPYTERHUB_API_TOKEN``.

    Raises:
        TesseraTokenError: When the variable is unset, meaning the code
            does not run inside a JupyterHub single-user server.
    """
    token = os.environ.get("JUPYTERHUB_API_TOKEN", "")
    if not token:
        raise TesseraTokenError(
            "not inside a JupyterHub single-user server ($JUPYTERHUB_API_TOKEN is not set)"
        )
    return token


def _service_base() -> str:
    """Return the tessera service base URL, with a trailing slash.

    Resolution cascade, highest priority first: ``TESSERA_URL`` when set,
    otherwise a value derived from ``JUPYTERHUB_PUBLIC_URL`` (its scheme
    and host) and ``JUPYTERHUB_BASE_URL`` (the Hub base path).

    Returns:
        The service base URL ending in ``services/tessera/``.

    Raises:
        TesseraTokenError: When neither source is available.
    """
    explicit = os.environ.get("TESSERA_URL", "").strip()
    if explicit:
        return explicit if explicit.endswith("/") else f"{explicit}/"
    public = os.environ.get("JUPYTERHUB_PUBLIC_URL", "").strip()
    if public:
        parts = urlsplit(public)
        base_path = os.environ.get("JUPYTERHUB_BASE_URL", "/")
        if not base_path.endswith("/"):
            base_path = f"{base_path}/"
        return f"{parts.scheme}://{parts.netloc}{base_path}services/tessera/"
    raise TesseraTokenError(
        "cannot locate the tessera service: set TESSERA_URL "
        "(or configure c.JupyterHub.public_url); see the tessera documentation"
    )


def _error_message(server: str, response: httpx.Response) -> str:
    """Build a token-free, actionable message from a non-200 response.

    Args:
        server: The server the request was for.
        response: The non-200 response returned by the service.

    Returns:
        A single-line diagnosis carrying the service ``detail`` (or the
        ``error`` slug, or a bounded slice of the body) and the status.
    """
    detail = ""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        raw = payload.get("detail") or payload.get("error") or ""
        detail = str(raw).strip()[:_ERROR_TEXT_LIMIT]
    if not detail:
        detail = response.text[:_ERROR_TEXT_LIMIT].strip()
    if not detail:
        detail = "the tessera service returned an error"
    return f"{server}: {detail} (HTTP {response.status_code})"


def get_token(server: str) -> str:
    """Return a fresh access token for a declared server.

    Each call performs exactly one request to the tessera service, which
    refreshes the token as needed. Nothing is cached kernel-side, so a
    token revoked at the provider is never served from a stale value.

    Args:
        server: The declared server name (the key shown in the panel).

    Returns:
        The current access token, as a string.

    Raises:
        TesseraTokenError: When no fresh token can be obtained: outside a
            single-user server, an unknown service URL, a network failure,
            or any non-200 answer. The message is actionable and carries
            no token value.

    Examples:
        >>> token = get_token("my-idp")  # doctest: +SKIP
    """
    api_token = _api_token()
    endpoint = f"{_service_base()}token?server={quote(server, safe='')}"
    headers = {"Authorization": f"token {api_token}"}
    try:
        with _new_client() as client:
            response = client.get(endpoint, headers=headers)
    except httpx.HTTPError as exc:
        raise TesseraTokenError(
            f"{server}: could not reach the tessera service ({type(exc).__name__})"
        ) from exc
    if response.status_code != 200:
        raise TesseraTokenError(_error_message(server, response))
    try:
        value = response.json()["access_token"]
    except (ValueError, KeyError, TypeError) as exc:
        raise TesseraTokenError(
            f"{server}: the tessera service returned an unexpected response"
        ) from exc
    return str(value)


class _TokenProxy:
    """Lazy mapping exposing ``TESSERA_TOKEN["<server>"]`` in a notebook.

    Subscripting a declared server name calls :func:`get_token` and
    returns a fresh access token. Nothing is cached: every subscript is a
    fresh request, so a revoked token is never handed out from a stale
    value. Only subscripting is supported; the proxy is not a full mapping
    (no iteration, no membership test).
    """

    def __getitem__(self, server: str) -> str:
        """Return a fresh access token for ``server``.

        Args:
            server: The declared server name.

        Returns:
            The current access token, as a string.

        Raises:
            TesseraTokenError: Propagated from :func:`get_token`.
        """
        return get_token(server)

    def __repr__(self) -> str:
        """Return a usage hint, never a token value."""
        return '<tessera tokens: use TESSERA_TOKEN["<server>"]>'


TESSERA_TOKEN = _TokenProxy()


def load_ipython_extension(ipython: _IPythonShell) -> None:
    """Inject ``TESSERA_TOKEN`` and ``get_token`` into the notebook.

    Called by ``%load_ext tessera.kernel`` or the ``InteractiveShellApp``
    auto-load, with the live IPython shell.

    Args:
        ipython: The live IPython ``InteractiveShell``.
    """
    ipython.push({"TESSERA_TOKEN": TESSERA_TOKEN, "get_token": get_token})


def unload_ipython_extension(ipython: _IPythonShell) -> None:
    """Remove the names injected by :func:`load_ipython_extension`.

    Args:
        ipython: The live IPython ``InteractiveShell``.
    """
    for name in _INJECTED_NAMES:
        ipython.user_ns.pop(name, None)
