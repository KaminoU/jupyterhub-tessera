"""HTTP tests for ``POST /verify``: the only 100%-reliable validity test.

Rejection-first: anonymous and cookie-without-XSRF callers get 403 before
anything else; token-authenticated callers are exempt from XSRF, exactly
like JupyterHub's own semantics. The answer is a bare boolean: a
definitive provider rejection IS the answer false (the record is purged
by the refresh path), while a transient failure is 502, never a false.
The cookie-with-real-signed-XSRF happy path requires the full Hub OAuth
handshake and is exercised on the live bench (e2e tier).
"""

from __future__ import annotations

import json
import os

from kstlib.auth.errors import TokenRefreshError
from support import SECRET_ENV_NAME, ServiceTestCase


class TestVerifyXsrfAndAuth(ServiceTestCase):
    """The route refuses anonymous and XSRF-less cookie callers with 403."""

    def test_anonymous_post_is_403(self) -> None:
        """No credentials and no XSRF material: refused outright."""
        response = self.fetch(
            self.service_url("verify", server="manual-idp"), method="POST", body=""
        )
        assert response.code == 403

    def test_cookie_without_xsrf_is_403(self) -> None:
        """A browser session without the XSRF header is refused."""
        self.seed_token()
        response = self.fetch(
            self.service_url("verify", server="manual-idp"),
            method="POST",
            body="",
            headers=self.cookie_headers(),
        )
        assert response.code == 403
        assert len(self.journal.refreshes) == 0  # refused before any provider call

    def test_cookie_with_wrong_xsrf_is_403(self) -> None:
        """A forged XSRF header never matches the Hub-signed token."""
        self.seed_token()
        headers = {**self.cookie_headers(), "X-XSRFToken": "forged-xsrf-value"}
        response = self.fetch(
            self.service_url("verify", server="manual-idp"),
            method="POST",
            body="",
            headers=headers,
        )
        assert response.code == 403
        assert len(self.journal.refreshes) == 0

    def test_token_auth_is_exempt_from_xsrf(self) -> None:
        """An API-token caller needs no XSRF token (JupyterHub semantics)."""
        self.seed_token()
        response = self.fetch(
            self.service_url("verify", server="manual-idp"),
            method="POST",
            body="",
            headers=self.token_headers(),
        )
        assert response.code == 200

    def test_get_method_is_not_allowed(self) -> None:
        """The action is POST-only."""
        response = self.fetch(
            self.service_url("verify", server="manual-idp"), headers=self.token_headers()
        )
        assert response.code == 405


class TestVerifyContract(ServiceTestCase):
    """The verify answer is an honest boolean; no token value ever leaves."""

    def _verify(self) -> tuple[int, dict[str, object]]:
        """POST /verify as a token-authenticated caller; return code and JSON."""
        response = self.fetch(
            self.service_url("verify", server="manual-idp"),
            method="POST",
            body="",
            headers=self.token_headers(),
        )
        return response.code, dict(json.loads(response.body))

    def test_valid_stored_token_reports_true(self) -> None:
        """A token the provider still refreshes verifies as true."""
        self.seed_token()
        code, payload = self._verify()
        assert code == 200
        assert payload == {"valid": True}
        assert len(self.journal.refreshes) == 1  # a real provider round-trip ran

    def test_response_never_carries_the_access_token(self) -> None:
        """The refresh runs for real but only the boolean leaves."""
        self.seed_token()
        response = self.fetch(
            self.service_url("verify", server="manual-idp"),
            method="POST",
            body="",
            headers=self.token_headers(),
        )
        assert response.headers["Cache-Control"] == "no-store"
        assert "at-fresh" not in response.body.decode()
        assert "rt-stored" not in response.body.decode()

    def test_definitive_rejection_reports_false_and_purges(self) -> None:
        """A rejected token is the answer false, and the status turns red."""
        self.journal.refresh_result = TokenRefreshError("invalid_grant", retryable=False)
        self.seed_token()
        code, payload = self._verify()
        assert code == 200
        assert payload == {"valid": False}
        status = self.fetch(
            self.service_url("status", server="manual-idp"), headers=self.token_headers()
        )
        assert json.loads(status.body)["has_token"] is False

    def test_transient_failure_is_502_and_keeps_record(self) -> None:
        """On a doubt the answer is 502, never false; the record survives."""
        self.journal.refresh_result = TokenRefreshError("gateway timeout", retryable=True)
        self.seed_token()
        code, payload = self._verify()
        assert code == 502
        assert payload["error"] == "refresh_unavailable"
        status = self.fetch(
            self.service_url("status", server="manual-idp"), headers=self.token_headers()
        )
        assert json.loads(status.body)["has_token"] is True

    def test_no_stored_token_is_409(self) -> None:
        """Nothing to verify yields the actionable JSON 409."""
        code, payload = self._verify()
        assert code == 409
        assert payload["error"] == "no_stored_token"

    def test_unknown_server_is_404(self) -> None:
        """An undeclared server name gets the JSON 404."""
        response = self.fetch(
            self.service_url("verify", server="nowhere"),
            method="POST",
            body="",
            headers=self.token_headers(),
        )
        assert response.code == 404
        assert json.loads(response.body)["error"] == "unknown_server"

    def test_missing_server_is_400(self) -> None:
        """The server parameter is mandatory, as JSON."""
        response = self.fetch(
            self.service_url("verify"), method="POST", body="", headers=self.token_headers()
        )
        assert response.code == 400

    def test_unresolvable_secret_is_500(self) -> None:
        """A lost client secret is a deployment error, honestly reported."""
        self.seed_token()
        os.environ.pop(SECRET_ENV_NAME, None)
        code, payload = self._verify()
        assert code == 500
        assert payload["error"] == "service_misconfigured"
