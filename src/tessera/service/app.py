"""Tornado application factory for the tessera JupyterHub Service."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from tornado.web import Application, RequestHandler

from tessera.service.handlers import (
    CallbackHandler,
    HealthHandler,
    HubCallbackHandler,
    LoginHandler,
    RevokeHandler,
    ServersHandler,
    StatusHandler,
    TokenHandler,
    VerifyHandler,
)

if TYPE_CHECKING:
    from jupyterhub.services.auth import HubAuth, HubOAuth

    from tessera.flow.orchestrator import FlowOrchestrator

log = logging.getLogger(__name__)


def _log_request_without_query(handler: RequestHandler) -> None:
    """Access log stripped of the query string (token hygiene).

    OAuth material (``state``, ``code``) travels in query parameters, so
    the default access line would persist it in the logs; this variant
    logs the method, the bare path, the status, and the timing only. A
    successful liveness probe on the bare prefix is demoted to DEBUG so
    its poll every interval stays out of the way; an error status there
    (a bad method, a scan burst) stays at INFO, as does every real
    endpoint and any genuine 404.

    Args:
        handler: The handler whose request just completed.
    """
    request = handler.request
    demoted = isinstance(handler, HealthHandler) and handler.get_status() < 400
    level = logging.DEBUG if demoted else logging.INFO
    log.log(
        level,
        "%d %s %s %.2fms",
        handler.get_status(),
        request.method,
        urlsplit(request.uri or "").path,
        1000.0 * request.request_time(),
    )


def _normalize_prefix(prefix: str) -> str:
    """Return the prefix with exactly one leading and one trailing slash.

    Args:
        prefix: The raw service prefix, usually the value of
            ``JUPYTERHUB_SERVICE_PREFIX``.

    Returns:
        The normalized prefix (``/`` when the input is empty or ``/``).
    """
    inner = prefix.strip("/")
    return f"/{inner}/" if inner else "/"


def make_app(
    orchestrator: FlowOrchestrator,
    *,
    prefix: str,
    hub_auth: HubAuth,
    hub_oauth: HubOAuth,
    cookie_secret: bytes,
) -> Application:
    """Build the tornado application serving the OAuth endpoints.

    Routes live under the JupyterHub service prefix, exactly as the Hub
    proxy forwards them. The bare prefix serves the unauthenticated
    liveness ``HealthHandler``. Browser-facing routes (``login``,
    ``callback``, ``status``, ``servers``, the ``verify`` and ``revoke``
    actions, and the Hub's own ``oauth_callback``) authenticate through
    ``hub_oauth``; the ``token`` route receives the plain token-only
    ``hub_auth``, so a browser cookie can never reach it.

    Args:
        orchestrator: The process-wide flow orchestrator.
        prefix: The public route prefix (``JUPYTERHUB_SERVICE_PREFIX``).
        hub_auth: The token-only authenticator, for ``token``.
        hub_oauth: The browser-capable authenticator, for the other routes.
        cookie_secret: The secret signing the service session cookies.

    Returns:
        The configured application, with XSRF protection enabled.
    """
    base = _normalize_prefix(prefix)
    browser = {"orchestrator": orchestrator, "hub_auth": hub_oauth}
    api = {"orchestrator": orchestrator, "hub_auth": hub_auth}
    return Application(
        [
            (base, HealthHandler),
            (f"{base}login", LoginHandler, browser),
            (f"{base}callback", CallbackHandler, browser),
            (f"{base}token", TokenHandler, api),
            (f"{base}status", StatusHandler, browser),
            (f"{base}servers", ServersHandler, browser),
            (f"{base}verify", VerifyHandler, browser),
            (f"{base}revoke", RevokeHandler, browser),
            (f"{base}oauth_callback", HubCallbackHandler, {"hub_auth": hub_oauth}),
        ],
        cookie_secret=cookie_secret,
        xsrf_cookies=True,
        log_function=_log_request_without_query,
    )
