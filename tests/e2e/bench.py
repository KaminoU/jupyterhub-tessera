"""HTTP harness for the end-to-end suite against the live local bench.

Everything here drives the same HTTP a browser would, with httpx cookie
jars only: the Hub login form (DummyAuthenticator), the Hub-to-service
OAuth handshake, the Keycloak login form (pure HTML, no JavaScript), and
the tessera callback. Every value used here is a public bench fixture
from ``infra/``; ephemeral Hub usernames give each run a pristine token
store. Tokens never appear in assertions or logs: comparisons go through
:func:`digest`.
"""

from __future__ import annotations

import hashlib
import html
import re
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx

from tessera.config import load_config

HUB_URL = "http://localhost:8000"
SERVICE_PREFIX = "/services/tessera"


@lru_cache(maxsize=1)
def keycloak_url() -> str:
    """Return the IdP base URL, derived from the bench source of truth.

    ``infra/servers.yml`` is the single place declaring where Keycloak
    listens (pinned by the tier-1 bench test), so the harness never
    hardcodes the port.
    """
    config = load_config(Path(__file__).resolve().parents[2] / "infra" / "servers.yml")
    issuer = config.get("stable-discovery").provider.issuer
    assert issuer is not None
    parts = urlsplit(issuer)
    return f"{parts.scheme}://{parts.netloc}"


IDP_PASSWORDS = {"alice": "alice-test-password", "bob": "bob-test-password"}

BENCH_HELP = (
    "bench not running: cd infra && docker compose up -d "
    "&& jupyterhub -f infra/jupyterhub_config.py"
)

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_MAX_HOPS = 15


def bench_is_up() -> bool:
    """Probe the Hub and the IdP; True when both answer 200."""
    try:
        hub = httpx.get(f"{HUB_URL}/hub/api", timeout=5)
        idp = httpx.get(
            f"{keycloak_url()}/realms/tessera-stable/.well-known/openid-configuration",
            timeout=5,
        )
    except httpx.HTTPError:
        return False
    return hub.status_code == 200 and idp.status_code == 200


def digest(value: str) -> str:
    """Return a short non-reversible fingerprint, safe to compare and print.

    Args:
        value: The secret-bearing string (an access token, typically).

    Returns:
        The first 12 hex chars of its SHA-256.
    """
    return hashlib.sha256(value.encode()).hexdigest()[:12]


class HubSession:
    """One authenticated Hub browser session with its own cookie jar.

    One instance per Hub user. The ephemeral username guarantees an
    initially empty token store and zero collision with manual bench
    sessions, so the suite can be replayed at will.
    """

    def __init__(self) -> None:
        """Open the session with a fresh ephemeral Hub username."""
        self.username = f"e2e-{secrets.token_hex(4)}"
        self.client = httpx.Client(base_url=HUB_URL, follow_redirects=False, timeout=30)

    def close(self) -> None:
        """Release the underlying HTTP client."""
        self.client.close()

    def _hub_xsrf(self) -> str:
        """Return the Hub's XSRF cookie value.

        The path filter matters: after the service handshake the jar also
        holds the service's own ``_xsrf`` cookie, and an unqualified
        lookup raises ``CookieConflict``.
        """
        return self.client.cookies.get("_xsrf", path="/hub/") or ""

    def login_hub(self) -> None:
        """Authenticate against the Hub login form (any password accepted)."""
        response = self.client.get("/hub/login")
        assert response.status_code == 200
        response = self.client.post(
            "/hub/login",
            data={
                "username": self.username,
                "password": "e2e-throwaway-password",
                "_xsrf": self._hub_xsrf(),
            },
        )
        assert response.status_code == 302

    def api_token(self) -> str:
        """Create a JupyterHub API token for this user.

        Returns:
            The token value (carries the user's scopes via the token role).
        """
        response = self.client.post(
            f"/hub/api/users/{self.username}/tokens",
            json={"note": "tessera e2e suite"},
            headers={"X-XSRFToken": self._hub_xsrf()},
        )
        assert response.status_code == 201
        token: str = response.json()["token"]
        return token

    def follow(self, start: str | httpx.Response, *, stop_at_idp: bool = False) -> httpx.Response:
        """Follow redirects manually, optionally stopping at the IdP boundary.

        Args:
            start: A URL to GET first, or an already-received response.
            stop_at_idp: When True, return the redirect whose Location
                points at Keycloak instead of following it.

        Returns:
            The first non-redirect response, or the IdP-bound redirect.
        """
        response = self.client.get(start) if isinstance(start, str) else start
        for _ in range(_MAX_HOPS):
            if response.status_code not in _REDIRECT_CODES:
                return response
            location = urljoin(str(response.request.url), response.headers["Location"])
            if stop_at_idp and location.startswith(keycloak_url()):
                return response
            response = self.client.get(location)
        raise AssertionError("redirect chain exceeded the hop bound")

    def status_json(self, server: str) -> dict[str, Any]:
        """Read the status endpoint as the browser button would (cookies).

        The first call also walks the Hub-to-service OAuth handshake,
        which sets the service session cookie in the jar.

        Args:
            server: The declared tessera server name.

        Returns:
            The status JSON payload.
        """
        response = self.follow(f"{SERVICE_PREFIX}/status?server={server}")
        assert response.status_code == 200
        payload: dict[str, Any] = response.json()
        return payload

    def _service_xsrf(self) -> str:
        """Return the service's XSRF cookie value.

        The service handshake (walked by the first :meth:`status_json`)
        sets a ``_xsrf`` cookie scoped to the service prefix; the panel
        echoes it in the standard ``X-XSRFToken`` header on every POST.
        """
        return self.client.cookies.get("_xsrf", path=f"{SERVICE_PREFIX}/") or ""

    def post_action(self, action: str, server: str) -> httpx.Response:
        """POST a panel action (verify or revoke) as the browser panel would.

        Args:
            action: The action route name (``verify`` or ``revoke``).
            server: The declared tessera server name.

        Returns:
            The raw response (asserted by the caller).
        """
        return self.client.post(
            f"{SERVICE_PREFIX}/{action}?server={server}",
            headers={"X-XSRFToken": self._service_xsrf()},
        )

    def token_endpoint(self, server: str, api_token: str) -> httpx.Response:
        """Call the token endpoint with an API token (never the cookies).

        Args:
            server: The declared tessera server name.
            api_token: The JupyterHub API token to authenticate with.

        Returns:
            The raw response (asserted by the caller).
        """
        return self.client.get(
            f"{SERVICE_PREFIX}/token?server={server}",
            headers={"Authorization": f"token {api_token}"},
        )

    def authorization_state(self, server: str) -> str:
        """Start a login and return tessera's state, without visiting the IdP.

        Args:
            server: The declared tessera server name.

        Returns:
            The ``state`` parameter of the identity-provider redirect.
        """
        response = self.follow(f"{SERVICE_PREFIX}/login?server={server}", stop_at_idp=True)
        location = urljoin(str(response.request.url), response.headers["Location"])
        return dict(parse_qsl(urlsplit(location).query))["state"]

    def complete_flow(self, server: str, idp_user: str) -> str:
        """Run the full sign-in against the real IdP; return the final page.

        Args:
            server: The declared tessera server name.
            idp_user: The Keycloak fixture user (``alice`` or ``bob``).

        Returns:
            The HTML of the final callback page.
        """
        form_page = self.follow(f"{SERVICE_PREFIX}/login?server={server}")
        assert form_page.status_code == 200
        action_match = re.search(r'action="([^"]+)"', form_page.text)
        assert action_match is not None, "keycloak login form not found"
        submitted = self.client.post(
            html.unescape(action_match.group(1)),
            data={"username": idp_user, "password": IDP_PASSWORDS[idp_user]},
        )
        final = self.follow(submitted)
        assert final.status_code == 200
        return final.text
