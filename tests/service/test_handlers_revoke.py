"""HTTP tests for ``POST /revoke``: provider best-effort, local purge always.

Rejection-first like every route. The response is honest: ``revoked`` is
true whenever the local record was purged, and ``provider_notified``
reports only a revocation the provider actually confirmed; a missing
revocation endpoint or a failed provider call never blocks the purge.
The cookie-with-real-signed-XSRF happy path is exercised on the live
bench (e2e tier), like for ``/verify``.
"""

from __future__ import annotations

import json

from support import ServiceTestCase


class TestRevokeXsrfAndAuth(ServiceTestCase):
    """The route refuses anonymous and XSRF-less cookie callers with 403."""

    def test_anonymous_post_is_403(self) -> None:
        """No credentials and no XSRF material: refused outright."""
        response = self.fetch(
            self.service_url("revoke", server="manual-idp"), method="POST", body=""
        )
        assert response.code == 403

    def test_cookie_without_xsrf_is_403_and_purges_nothing(self) -> None:
        """A browser session without the XSRF header is refused; the record stays."""
        self.seed_token()
        response = self.fetch(
            self.service_url("revoke", server="manual-idp"),
            method="POST",
            body="",
            headers=self.cookie_headers(),
        )
        assert response.code == 403
        status = self.fetch(
            self.service_url("status", server="manual-idp"), headers=self.token_headers()
        )
        assert json.loads(status.body)["has_token"] is True

    def test_get_method_is_not_allowed(self) -> None:
        """The action is POST-only."""
        response = self.fetch(
            self.service_url("revoke", server="manual-idp"), headers=self.token_headers()
        )
        assert response.code == 405


class TestRevokeContract(ServiceTestCase):
    """The revoke response reports the purge and the honest provider outcome."""

    def _revoke(self) -> tuple[int, dict[str, object]]:
        """POST /revoke as a token-authenticated caller; return code and JSON."""
        response = self.fetch(
            self.service_url("revoke", server="manual-idp"),
            method="POST",
            body="",
            headers=self.token_headers(),
        )
        return response.code, dict(json.loads(response.body))

    def test_revoke_notifies_provider_and_purges(self) -> None:
        """A confirmed provider revocation reports notified and turns red."""
        self.seed_token()
        code, payload = self._revoke()
        assert code == 200
        assert payload == {"revoked": True, "provider_notified": True}
        assert len(self.journal.revokes) == 1
        status = self.fetch(
            self.service_url("status", server="manual-idp"), headers=self.token_headers()
        )
        assert json.loads(status.body)["has_token"] is False

    def test_unconfirmed_provider_revocation_still_purges(self) -> None:
        """A provider that could not be notified never blocks the purge."""
        self.journal.revoke_result = False
        self.seed_token()
        code, payload = self._revoke()
        assert code == 200
        assert payload == {"revoked": True, "provider_notified": False}
        status = self.fetch(
            self.service_url("status", server="manual-idp"), headers=self.token_headers()
        )
        assert json.loads(status.body)["has_token"] is False

    def test_second_revoke_is_409(self) -> None:
        """Revoking twice: the second call finds nothing and gets the 409."""
        self.seed_token()
        first_code, _ = self._revoke()
        assert first_code == 200
        second_code, payload = self._revoke()
        assert second_code == 409
        assert payload["error"] == "no_stored_token"

    def test_no_stored_token_is_409(self) -> None:
        """Nothing to revoke yields the actionable JSON 409."""
        code, payload = self._revoke()
        assert code == 409
        assert payload["error"] == "no_stored_token"

    def test_unknown_server_is_404(self) -> None:
        """An undeclared server name gets the JSON 404."""
        response = self.fetch(
            self.service_url("revoke", server="nowhere"),
            method="POST",
            body="",
            headers=self.token_headers(),
        )
        assert response.code == 404
        assert json.loads(response.body)["error"] == "unknown_server"

    def test_missing_server_is_400(self) -> None:
        """The server parameter is mandatory, as JSON."""
        response = self.fetch(
            self.service_url("revoke"), method="POST", body="", headers=self.token_headers()
        )
        assert response.code == 400

    def test_response_never_carries_a_token_value(self) -> None:
        """The stored refresh token never appears in the response."""
        self.seed_token(refresh_token="rt-stored-secret")
        response = self.fetch(
            self.service_url("revoke", server="manual-idp"),
            method="POST",
            body="",
            headers=self.token_headers(),
        )
        assert response.headers["Cache-Control"] == "no-store"
        assert "rt-stored-secret" not in response.body.decode()
