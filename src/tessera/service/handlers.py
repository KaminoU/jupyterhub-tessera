"""Tornado request handlers for the tessera JupyterHub Service.

Seven authenticated endpoints expose the OAuth flow to JupyterLab:
``/login`` starts the authorization-code flow and redirects to the
identity provider; ``/callback`` receives the provider's redirect and
stores the refresh token; ``/token`` returns a fresh access token to API
clients; ``/status`` is the cheap per-server detail polled by the
frontend panel; ``/servers`` is the static button topology the panel
loads once at startup; ``POST /verify`` answers whether the stored token
is really still accepted by the provider; ``POST /revoke`` notifies the
provider best-effort and always purges the stored token. The bare prefix
serves an unauthenticated, minimal liveness probe. The Hub's own
``/oauth_callback`` completes the browser login with the Hub itself.

Identity always comes from JupyterHub authentication (the Hub auth
mixins), never from a request parameter. Browser-facing routes accept
the Hub OAuth cookie or an API token; ``/token`` is token-only by
construction (its plain ``HubAuth`` cannot read cookies), so a browser
session alone can never extract a token: structural CSRF protection.
Only Hub *users* are served: a service identity would collide with the
per-username namespace of the token store and is rejected.

Every error response is static: no state, code, token, secret, or
received parameter value is ever echoed in a body, a header, or a log
line, even at DEBUG.
"""

from __future__ import annotations

import logging
from http.client import responses
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from jupyterhub.services.auth import (
    HubAuth,
    HubOAuth,
    HubOAuthCallbackHandler,
    HubOAuthenticated,
    UserNotAllowed,
)
from tornado import web

from tessera.flow.errors import (
    CallbackValidationError,
    ExchangeError,
    ExchangeRejectedError,
    NoStoredTokenError,
    PendingFlowStoreFull,
    RefreshRejectedError,
    SecretResolutionError,
    UnknownServerError,
)

if TYPE_CHECKING:
    from types import TracebackType

    from tessera.flow.orchestrator import FlowOrchestrator

log = logging.getLogger(__name__)

MAX_SERVER_NAME_LEN = 256

_CSP = "default-src 'none'; style-src 'unsafe-inline'"

_PAGE_STYLE = (
    "body{font-family:system-ui,sans-serif;margin:15vh auto;"
    "max-width:28rem;padding:0 1rem;text-align:center;color:#222}"
)

_MSG_PROVIDER_REJECTED = (
    "The identity provider returned an error for this sign-in. "
    "Close this tab and click the tessera button again; if this keeps "
    "happening, contact your JupyterHub administrator."
)
_MSG_PROVIDER_FAILED = (
    "The sign-in could not be completed with the identity provider. "
    "Close this tab and try again shortly; if this persists, contact "
    "your JupyterHub administrator."
)

_HTML_ERROR_MESSAGES = {
    400: (
        "The sign-in request was invalid or has expired. "
        "Close this tab and click the tessera button again."
    ),
    403: "You are not allowed to use this service.",
    404: "This server is not declared in the tessera configuration.",
    500: "The service is misconfigured. Contact your JupyterHub administrator.",
    502: _MSG_PROVIDER_FAILED,
    503: ("Too many sign-ins are in progress. Close this tab and try again shortly."),
}


def _page(title: str, message: str) -> str:
    """Render a sober, static HTML page (no script, no echoed value).

    Args:
        title: The page heading; always one of tessera's own constants.
        message: The body text; always one of tessera's own constants.

    Returns:
        The complete HTML document.
    """
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        '<head><meta charset="utf-8"><title>tessera</title>'
        f"<style>{_PAGE_STYLE}</style></head>\n"
        f"<body><h1>{title}</h1><p>{message}</p></body>\n"
        "</html>"
    )


class _TokenSafeExceptionLog:
    """Mixin: log uncaught exceptions without echoing the request query.

    Tornado's default ``log_exception`` records the full request line and
    the request repr, both of which carry the OAuth ``code`` and ``state``
    from the query string. This override keeps the diagnostic value
    (method, bare path, exception type, and the stack via ``exc_info``)
    while dropping every received value, and routes it onto the greppable
    tessera logger. A mapped ``HTTPError`` is a status-only rejection here,
    so it is left silent exactly as tornado leaves a message-free
    ``HTTPError``; its ``_request_summary`` (which would echo the URI) is
    never reached.
    """

    def log_exception(
        self,
        typ: type[BaseException] | None,
        value: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Log an uncaught exception, echoing no received value.

        Args:
            typ: The exception type, as passed by tornado.
            value: The exception instance being reported.
            tb: The traceback; the value's own ``exc_info`` carries it.
        """
        if isinstance(value, web.HTTPError):
            return
        request = cast(web.RequestHandler, self).request
        log.error(
            "uncaught exception on %s %s (%s)",
            request.method,
            urlsplit(request.uri or "").path,
            type(value).__name__,
            exc_info=value,
        )


class _TesseraHandler(_TokenSafeExceptionLog, HubOAuthenticated, web.RequestHandler):
    """Base handler wiring Hub authentication and the flow orchestrator.

    Handlers receive their collaborators through ``initialize`` (the
    application factory passes them per route): the orchestrator and the
    ``HubAuth`` flavor that decides which credentials the route accepts
    (``HubOAuth`` for browser routes, plain ``HubAuth`` for the
    token-only route). The Hub identity is resolved in the asynchronous
    ``prepare``, so the event loop never blocks on a Hub API round-trip.
    """

    def initialize(self, orchestrator: FlowOrchestrator, hub_auth: HubAuth) -> None:
        """Receive the per-route collaborators from the application factory.

        Args:
            orchestrator: The process-wide flow orchestrator.
            hub_auth: The Hub authenticator for this route.
        """
        self._orchestrator = orchestrator
        self.hub_auth = hub_auth

    async def prepare(self) -> None:
        """Resolve the Hub identity off the synchronous path.

        The upstream mixin resolves the identity lazily inside its
        synchronous ``get_current_user``, blocking on a background
        thread; awaiting the asynchronous variant here caches the model
        on the handler first, so the later synchronous call is a pure
        cache hit.
        """
        await self.hub_auth.get_user(self, sync=False)

    def check_hub_user(self, model: dict[str, Any]) -> dict[str, Any] | None:
        """Allow only Hub users, after the upstream scope check.

        A service identity carries no browser session and would collide
        with the per-username namespace of the token store, so it is
        rejected outright.

        Args:
            model: The identity model returned by the Hub API.

        Returns:
            The model when the identity is allowed.

        Raises:
            UserNotAllowed: If the identity is not a Hub user (the
                upstream mixin maps it to a 403 response).
        """
        allowed: dict[str, Any] | None = super().check_hub_user(model)
        if allowed is not None and allowed.get("kind", "user") != "user":
            log.warning("[SECURITY] non-user identity rejected on %s", self.request.path)
            raise UserNotAllowed(model)
        return allowed

    def _username(self) -> str:
        """Return the authenticated Hub username.

        The identity comes from the authentication layer only, never
        from a request parameter; callers run under ``@web.authenticated``
        so the user model is always present here.

        Returns:
            The Hub username.
        """
        return str(self.current_user["name"])

    def _server_argument(self) -> str:
        """Read and harden the ``server`` query parameter.

        Returns:
            The stripped server name.

        Raises:
            web.HTTPError: 400 when the parameter is missing, blank, or
                oversized; the received value is never echoed.
        """
        server = self.get_argument("server", "").strip()
        if not server or len(server) > MAX_SERVER_NAME_LEN:
            log.warning("[SECURITY] missing or oversized 'server' parameter, rejected")
            raise web.HTTPError(400)
        return server


class _HtmlPageHandler(_TesseraHandler):
    """Browser-facing base: sober static pages and hardened headers."""

    def set_default_headers(self) -> None:
        """Keep browser responses out of caches and lock down content."""
        self.set_header("Cache-Control", "no-store")
        self.set_header("Content-Security-Policy", _CSP)

    def write_error(self, status_code: int, **kwargs: object) -> None:
        """Render the static error page for the status, echoing nothing.

        Args:
            status_code: The HTTP status being reported.
            **kwargs: Tornado's error context; deliberately unused so no
                exception detail can leak into the page.
        """
        message = _HTML_ERROR_MESSAGES.get(status_code, "The request could not be processed.")
        self.finish(_page(f"Sign-in failed ({status_code})", message))

    def _error_page(self, status: int, message: str) -> None:
        """Reply with the sober static error page for an explicit case.

        Used when one status carries several distinct realities (the
        ``write_error`` table is keyed by status alone).

        Args:
            status: The HTTP status code.
            message: A static message; never a received value.
        """
        self.set_status(status)
        self.finish(_page(f"Sign-in failed ({status})", message))


class _JsonHandler(_TesseraHandler):
    """API-facing base: JSON bodies, no-store, JSON errors without traces."""

    def set_default_headers(self) -> None:
        """Keep token-bearing responses out of every cache."""
        self.set_header("Cache-Control", "no-store")

    def write_error(self, status_code: int, **kwargs: object) -> None:
        """Emit a minimal JSON error, without stack traces or echoes.

        Args:
            status_code: The HTTP status being reported.
            **kwargs: Tornado's error context; deliberately unused so no
                exception detail can leak into the body.
        """
        self.finish({"error": "http_error", "detail": responses.get(status_code, "error")})

    def _json_error(self, status: int, error: str, detail: str) -> None:
        """Reply with a machine-actionable JSON error.

        Args:
            status: The HTTP status code.
            error: A stable error slug for programmatic handling.
            detail: A static, actionable message; never a received value.
        """
        self.set_status(status)
        self.finish({"error": error, "detail": detail})


class LoginHandler(_HtmlPageHandler):
    """Starts the authorization-code flow: ``GET /login?server=<name>``.

    Accepts the Hub OAuth cookie (browser navigation) or an API token,
    and replies with a 302 redirect to the identity provider.
    """

    @web.authenticated
    async def get(self) -> None:
        """Begin the flow for the authenticated user and redirect."""
        server = self._server_argument()
        try:
            url = await self._orchestrator.begin_login(self._username(), server)
        except UnknownServerError as exc:
            raise web.HTTPError(404) from exc
        except PendingFlowStoreFull as exc:
            raise web.HTTPError(503) from exc
        except ExchangeError as exc:
            raise web.HTTPError(502) from exc
        except SecretResolutionError as exc:
            raise web.HTTPError(500) from exc
        self.redirect(url, status=302)


class CallbackHandler(_HtmlPageHandler):
    """Receives the provider redirect: ``GET /callback?code=&state=``.

    The user's browser lands here coming back from the identity
    provider, so the route authenticates with the Hub OAuth cookie. On
    success it stores the refresh token and shows a static confirmation
    page; every failure maps to a static error page that reveals nothing
    about the received values.
    """

    @web.authenticated
    async def get(self) -> None:
        """Validate the callback, exchange the code, store the token."""
        if self.get_argument("error", ""):
            log.warning("[SECURITY] provider returned an error on the callback, rejected")
            raise web.HTTPError(400)
        state = self.get_argument("state", "")
        code = self.get_argument("code", "")
        try:
            await self._orchestrator.complete_callback(self._username(), state, code)
        except (CallbackValidationError, UnknownServerError) as exc:
            # An unknown server here means the flow outlived the declared
            # configuration: same remedy as any stale flow, start again.
            raise web.HTTPError(400) from exc
        except ExchangeRejectedError:
            self._error_page(502, _MSG_PROVIDER_REJECTED)
            return
        except ExchangeError:
            self._error_page(502, _MSG_PROVIDER_FAILED)
            return
        except SecretResolutionError as exc:
            raise web.HTTPError(500) from exc
        self.finish(
            _page(
                "Signed in",
                "The token was stored. You can close this tab and return to JupyterLab.",
            )
        )


class TokenHandler(_JsonHandler):
    """Returns a fresh access token: ``GET /token?server=<name>``.

    Token-only by construction: the application factory injects a plain
    ``HubAuth``, which cannot read browser cookies, so a cookie session
    alone can never reach a token (structural CSRF protection). Meant
    for the kernel-side client authenticating with its JupyterHub API
    token.
    """

    @web.authenticated
    async def get(self) -> None:
        """Refresh and return the access token for the (user, server) pair."""
        server = self._server_argument()
        try:
            token = await self._orchestrator.get_access_token(self._username(), server)
        except UnknownServerError:
            self._json_error(
                404,
                "unknown_server",
                "This server is not declared in the tessera configuration.",
            )
            return
        except NoStoredTokenError:
            self._json_error(
                409,
                "no_stored_token",
                "No token is stored for this server: sign in with the tessera "
                "button in JupyterLab.",
            )
            return
        except RefreshRejectedError:
            self._json_error(
                409,
                "refresh_rejected",
                "The server rejected the stored token: sign in again with the "
                "tessera button in JupyterLab.",
            )
            return
        except ExchangeError:
            self._json_error(
                502,
                "refresh_unavailable",
                "The provider could not refresh the token. Retry shortly.",
            )
            return
        except SecretResolutionError:
            self._json_error(
                500,
                "service_misconfigured",
                "The service could not resolve the provider credentials. "
                "Contact your JupyterHub administrator.",
            )
            return
        self.finish(
            {
                "access_token": token.value,
                "expires_at": token.expires_at,
                "scope": token.scope,
            }
        )


class StatusHandler(_JsonHandler):
    """Reports the stored-token status: ``GET /status?server=<name>``.

    Cheap by design (one store read, no provider interaction), polled by
    the frontend panel; accepts the Hub OAuth cookie or an API token.
    ``has_token`` is the button's truth; the other fields are the detail
    the panel renders (timestamps, granted scope, refresh kind and dated
    refresh expiry). No token value ever appears here.
    """

    @web.authenticated
    async def get(self) -> None:
        """Report token presence and its detail fields for the pair."""
        server = self._server_argument()
        try:
            status = await self._orchestrator.get_status(self._username(), server)
        except UnknownServerError:
            self._json_error(
                404,
                "unknown_server",
                "This server is not declared in the tessera configuration.",
            )
            return
        self.finish(
            {
                "has_token": status.has_token,
                "expires_at": status.expires_at,
                "created_at": status.created_at,
                "updated_at": status.updated_at,
                "scope": status.scope,
                "refresh_kind": status.refresh_kind,
                "refresh_expires_at": status.refresh_expires_at,
            }
        )


class ServersHandler(_JsonHandler):
    """Lists the declared servers: ``GET /servers``.

    The static button topology the panel loads once at startup: name,
    display label, and state colors per declared server, in declaration
    order. A pure configuration projection: no store read, no provider
    interaction, and nothing secret or provider-facing in the payload.
    """

    @web.authenticated
    async def get(self) -> None:
        """List the declared servers' button descriptors."""
        self.finish(
            {
                "servers": [
                    {
                        "name": info.name,
                        "label": info.label,
                        "color_valid": info.color_valid,
                        "color_invalid": info.color_invalid,
                    }
                    for info in self._orchestrator.list_servers()
                ]
            }
        )


class _JsonPostHandler(_JsonHandler):
    """JSON POST base: JupyterHub's XSRF check, enforced deterministically.

    tornado runs ``check_xsrf_cookie`` before ``prepare``, i.e. before
    the Hub identity is resolved (so the token-authentication exemption
    cannot apply yet) and before JupyterHub's lazy handler patch is
    guaranteed installed (a cold process whose first request is a POST
    would run tornado's own check and refuse valid API callers). The
    tornado-time check is therefore neutralized and JupyterHub's check
    runs in ``prepare``, right after the identity resolution: cookie
    sessions must present the standard ``X-XSRFToken`` header matching
    the Hub-signed ``_xsrf`` cookie, token-authenticated calls are
    exempt, and a refusal is the same 403 either way.
    """

    # The browser-capable authenticator is injected on POST routes; the
    # annotation refines the base for the check delegation below.
    hub_auth: HubOAuth

    def check_xsrf_cookie(self) -> None:
        """Defer the XSRF check to prepare (see class docstring)."""

    async def prepare(self) -> None:
        """Resolve the Hub identity, then enforce JupyterHub's XSRF check."""
        await super().prepare()
        self.hub_auth.check_xsrf_cookie(self)


class VerifyHandler(_JsonPostHandler):
    """Tests the stored token for real: ``POST /verify?server=<name>``.

    The panel cannot call ``/token`` (token-only by construction), so
    this is its one reliable validity test: the same refresh round-trip,
    with only the verdict leaving. A definitive provider rejection is
    the answer ``{"valid": false}`` (the record is already purged, the
    next status poll turns red), not an error; a transient failure is a
    502, because a doubt is never reported as invalid. On a provider
    that rotates refresh tokens, a successful verify consumes a real
    refresh and the rotated token is persisted. Concurrent calls
    coalesce: a double-click costs one provider round-trip.
    """

    @web.authenticated
    async def post(self) -> None:
        """Answer whether the stored token is still accepted upstream."""
        server = self._server_argument()
        try:
            valid = await self._orchestrator.verify(self._username(), server)
        except UnknownServerError:
            self._json_error(
                404,
                "unknown_server",
                "This server is not declared in the tessera configuration.",
            )
            return
        except NoStoredTokenError:
            self._json_error(
                409,
                "no_stored_token",
                "No token is stored for this server: sign in with the tessera "
                "button in JupyterLab.",
            )
            return
        except ExchangeError:
            self._json_error(
                502,
                "refresh_unavailable",
                "The provider could not be reached to verify the token. Retry shortly.",
            )
            return
        except SecretResolutionError:
            self._json_error(
                500,
                "service_misconfigured",
                "The service could not resolve the provider credentials. "
                "Contact your JupyterHub administrator.",
            )
            return
        self.finish({"valid": valid})


class RevokeHandler(_JsonPostHandler):
    """Revokes the stored token: ``POST /revoke?server=<name>``.

    Provider first, best-effort; local purge always. ``revoked`` is true
    whenever the stored record was purged; ``provider_notified`` reports
    only a revocation the provider actually confirmed (false when no
    revocation endpoint is known or the provider call failed: the purge
    still happened, and the response says exactly what was achieved).
    """

    @web.authenticated
    async def post(self) -> None:
        """Revoke the stored token and report the honest outcome."""
        server = self._server_argument()
        try:
            notified = await self._orchestrator.revoke(self._username(), server)
        except UnknownServerError:
            self._json_error(
                404,
                "unknown_server",
                "This server is not declared in the tessera configuration.",
            )
            return
        except NoStoredTokenError:
            self._json_error(
                409,
                "no_stored_token",
                "No token is stored for this server: there is nothing to revoke.",
            )
            return
        self.finish({"revoked": True, "provider_notified": notified})


class HealthHandler(web.RequestHandler):
    """Answers a liveness probe on the bare prefix: ``GET {prefix}``.

    Deliberately unauthenticated and minimal: the Hub/proxy polls the
    service root to check it is alive, so this answers a small static
    body instead of a 404 (which flooded the logs every poll interval).
    The body carries nothing sensitive: no token, no secret, no
    configuration, no version. Its access-log line is demoted to DEBUG
    by the application factory so the poll traffic stays out of the way.
    """

    def set_default_headers(self) -> None:
        """Keep the probe response out of every cache."""
        self.set_header("Cache-Control", "no-store")

    async def get(self) -> None:
        """Answer the minimal liveness body."""
        self.finish({"service": "tessera", "status": "ok"})


class HubCallbackHandler(_TokenSafeExceptionLog, HubOAuthCallbackHandler):
    """Completes the browser's OAuth handshake with the Hub itself.

    Registered at ``{prefix}oauth_callback``: this is the Hub-to-service
    login that sets the service session cookie, not tessera's identity
    provider callback (``{prefix}callback``).
    """

    def initialize(self, hub_auth: HubOAuth) -> None:
        """Receive the shared browser authenticator from the factory.

        Args:
            hub_auth: The browser-facing Hub authenticator.
        """
        self.hub_auth = hub_auth
