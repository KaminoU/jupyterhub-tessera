"""HTTP tests for ``/status``: the per-server detail the panel polls.

The contract is frozen: ``has_token`` is the truth, ``expires_at`` is
indicative, and the detail fields (timestamps, scope, refresh kind and
expiry) come from the same single store read. Both credentials work; a
cookie presented by a cross-origin fetch without an XSRF token is
refused, proving the XSRF layer is live.
"""

from __future__ import annotations

import json

from support import LOWPRIV_TOKEN, ServiceTestCase

RED_STATUS = {
    "has_token": False,
    "expires_at": None,
    "created_at": None,
    "updated_at": None,
    "scope": None,
    "refresh_kind": "unknown",
    "refresh_expires_at": None,
}


class TestStatusContract(ServiceTestCase):
    """The status reports stored-token presence and its detail fields."""

    def test_no_stored_token_reports_red(self) -> None:
        """Without a stored token the status is red with every detail None."""
        response = self.fetch(
            self.service_url("status", server="manual-idp"), headers=self.token_headers()
        )
        assert response.code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert json.loads(response.body) == RED_STATUS

    def test_stored_token_reports_green_with_details(self) -> None:
        """A stored token reports green with the full stored detail."""
        self.seed_token(
            expires_at=500.0,
            scope="openid profile offline_access",
            refresh_expires_at=900.0,
        )
        response = self.fetch(
            self.service_url("status", server="manual-idp"), headers=self.token_headers()
        )
        payload = json.loads(response.body)
        assert payload["has_token"] is True
        assert payload["expires_at"] == 500.0
        assert payload["scope"] == "openid profile offline_access"
        assert payload["refresh_kind"] == "offline"
        assert payload["refresh_expires_at"] == 900.0
        assert isinstance(payload["created_at"], float)
        assert isinstance(payload["updated_at"], float)

    def test_session_scope_reports_session_kind(self) -> None:
        """A scope without offline_access reports the session refresh kind."""
        self.seed_token(scope="openid profile")
        response = self.fetch(
            self.service_url("status", server="manual-idp"), headers=self.token_headers()
        )
        payload = json.loads(response.body)
        assert payload["refresh_kind"] == "session"
        assert payload["refresh_expires_at"] is None

    def test_absent_scope_reports_unknown_kind(self) -> None:
        """A stored token without a scope reports the unknown refresh kind."""
        self.seed_token()
        payload = json.loads(
            self.fetch(
                self.service_url("status", server="manual-idp"), headers=self.token_headers()
            ).body
        )
        assert payload["has_token"] is True
        assert payload["refresh_kind"] == "unknown"

    def test_cookie_auth_works_for_the_button(self) -> None:
        """A browser session reads the status like a token does."""
        response = self.fetch(
            self.service_url("status", server="manual-idp"), headers=self.cookie_headers()
        )
        assert response.code == 200
        assert json.loads(response.body) == RED_STATUS

    def test_token_auth_is_exempt_from_xsrf_even_cross_fetch(self) -> None:
        """Token credentials never need an XSRF token, whatever the fetch mode."""
        headers = {
            **self.token_headers(),
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        response = self.fetch(self.service_url("status", server="manual-idp"), headers=headers)
        assert response.code == 200


class TestStatusRejections(ServiceTestCase):
    """The status route refuses what every other route refuses."""

    def test_unauthenticated_redirects_to_hub_login(self) -> None:
        """No credentials: the poller is sent to the Hub OAuth login."""
        response = self.fetch(
            self.service_url("status", server="manual-idp"), follow_redirects=False
        )
        assert response.code == 302
        assert response.headers["Location"].startswith("/hub/api/oauth2/authorize")

    def test_cookie_with_cors_fetch_requires_xsrf(self) -> None:
        """A cookie on a cross-origin style fetch is refused without XSRF.

        This is the live proof that the JupyterHub XSRF layer guards the
        cookie path: the same request with an ``unspecified`` fetch mode
        authenticates (see the contract tests above).
        """
        self.seed_token()
        headers = {
            **self.cookie_headers(),
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        response = self.fetch(
            self.service_url("status", server="manual-idp"),
            headers=headers,
            follow_redirects=False,
        )
        assert response.code == 302  # cookie refused: back to the Hub login

    def test_insufficient_scopes_rejected(self) -> None:
        """A user without the service access scope gets 403."""
        response = self.fetch(
            self.service_url("status", server="manual-idp"),
            headers=self.token_headers(LOWPRIV_TOKEN),
        )
        assert response.code == 403

    def test_unknown_server_is_404_json(self) -> None:
        """An undeclared server name gets a JSON 404."""
        response = self.fetch(
            self.service_url("status", server="nowhere"), headers=self.token_headers()
        )
        assert response.code == 404
        assert json.loads(response.body)["error"] == "unknown_server"

    def test_missing_server_is_400_json(self) -> None:
        """The server parameter is mandatory, as JSON."""
        response = self.fetch(self.service_url("status"), headers=self.token_headers())
        assert response.code == 400
        assert json.loads(response.body)["error"] == "http_error"
