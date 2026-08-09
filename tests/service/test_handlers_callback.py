"""HTTP tests for ``/callback``: identity binding, hardening, sober pages.

Rejections first: the callback burns and refuses a state presented by
another user, refuses unknown or missing material, never echoes a
received value, and maps provider failures to 502. The happy path
proves the full browser round-trip stores the token.
"""

from __future__ import annotations

import json
import os

from kstlib.auth.errors import TokenExchangeError
from support import BOB_TOKEN, CLIENT_SECRET, SECRET_ENV_NAME, ServiceTestCase


class TestCallbackHappyPath(ServiceTestCase):
    """A valid callback stores the token and shows a sober static page."""

    def test_full_browser_flow_stores_token(self) -> None:
        """Login then callback: the exchange runs and the status turns green."""
        state = self.begin_login(self.cookie_headers())
        response = self.fetch(
            f"{self.service_url('callback')}?code=auth-code-1&state={state}",
            headers=self.cookie_headers(),
            follow_redirects=False,
        )
        assert response.code == 200
        body = response.body.decode()
        assert "close this tab" in body
        assert "<script" not in body.lower()
        assert state not in body
        assert "auth-code-1" not in body
        code, verifier = self.journal.exchanges[0]
        assert code == "auth-code-1"
        assert verifier is not None
        status = self.fetch(
            self.service_url("status", server="manual-idp"), headers=self.token_headers()
        )
        payload = json.loads(status.body)
        assert payload["has_token"] is True
        assert payload["expires_at"] is None

    def test_success_page_carries_hardened_headers(self) -> None:
        """The confirmation page is uncacheable and CSP-locked."""
        state = self.begin_login(self.cookie_headers())
        response = self.fetch(
            f"{self.service_url('callback')}?code=auth-code-1&state={state}",
            headers=self.cookie_headers(),
            follow_redirects=False,
        )
        assert response.headers["Cache-Control"] == "no-store"
        assert "default-src 'none'" in response.headers["Content-Security-Policy"]


class TestCallbackRejections(ServiceTestCase):
    """Every invalid callback is refused with a static page."""

    def test_state_of_another_user_is_rejected_and_burned(self) -> None:
        """Bob presenting alice's state is refused; the flow is consumed."""
        state = self.begin_login(self.cookie_headers())
        stolen = self.fetch(
            f"{self.service_url('callback')}?code=auth-code-1&state={state}",
            headers=self.cookie_headers(BOB_TOKEN),
            follow_redirects=False,
        )
        assert stolen.code == 400
        retry = self.fetch(
            f"{self.service_url('callback')}?code=auth-code-1&state={state}",
            headers=self.cookie_headers(),
            follow_redirects=False,
        )
        assert retry.code == 400  # one-shot: the probe burned the pending flow
        assert self.journal.exchanges == []
        status = self.fetch(
            self.service_url("status", server="manual-idp"), headers=self.token_headers()
        )
        assert json.loads(status.body)["has_token"] is False

    def test_unknown_state_rejected(self) -> None:
        """A state that was never issued is refused."""
        response = self.fetch(
            f"{self.service_url('callback')}?code=auth-code-1&state=never-issued",
            headers=self.cookie_headers(),
            follow_redirects=False,
        )
        assert response.code == 400

    def test_missing_code_rejected(self) -> None:
        """A callback without an authorization code is refused."""
        state = self.begin_login(self.cookie_headers())
        response = self.fetch(
            f"{self.service_url('callback')}?state={state}",
            headers=self.cookie_headers(),
            follow_redirects=False,
        )
        assert response.code == 400

    def test_provider_error_param_is_400_without_echo(self) -> None:
        """A provider error redirect is refused; its content is never echoed."""
        response = self.fetch(
            f"{self.service_url('callback')}?error=access_denied&error_description=oops",
            headers=self.cookie_headers(),
            follow_redirects=False,
        )
        assert response.code == 400
        body = response.body.decode()
        assert "access_denied" not in body
        assert "oops" not in body

    def test_exchange_failure_is_502_with_honest_combined_message(self) -> None:
        """A failed interaction is 502 with the could-not-complete page."""
        self.journal.exchange_result = TokenExchangeError("Network error: boom")
        state = self.begin_login(self.cookie_headers())
        response = self.fetch(
            f"{self.service_url('callback')}?code=auth-code-1&state={state}",
            headers=self.cookie_headers(),
            follow_redirects=False,
        )
        assert response.code == 502
        body = response.body.decode()
        assert "could not be completed" in body
        assert "returned an error" not in body
        assert "could not be reached" not in body  # the old, factually wrong claim
        status = self.fetch(
            self.service_url("status", server="manual-idp"), headers=self.token_headers()
        )
        assert json.loads(status.body)["has_token"] is False

    def test_provider_rejection_is_502_with_returned_error_page(self) -> None:
        """A provider that answered an error gets its own static page."""
        # A provider-answered rejection always carries the HTTP status in
        # the kstlib contract; a definitive 4xx is not retryable.
        self.journal.exchange_result = TokenExchangeError(
            "Offline tokens not allowed", error_code="not_allowed", status_code=400
        )
        state = self.begin_login(self.cookie_headers())
        response = self.fetch(
            f"{self.service_url('callback')}?code=auth-code-1&state={state}",
            headers=self.cookie_headers(),
            follow_redirects=False,
        )
        assert response.code == 502
        body = response.body.decode()
        assert "returned an error" in body
        assert "could not be completed" not in body
        assert "not_allowed" not in body
        assert "Offline tokens" not in body
        status = self.fetch(
            self.service_url("status", server="manual-idp"), headers=self.token_headers()
        )
        assert json.loads(status.body)["has_token"] is False

    def test_unresolvable_secret_is_500(self) -> None:
        """A secret lost between login and callback is a deployment error."""
        state = self.begin_login(self.cookie_headers())
        os.environ.pop(SECRET_ENV_NAME, None)
        response = self.fetch(
            f"{self.service_url('callback')}?code=auth-code-1&state={state}",
            headers=self.cookie_headers(),
            follow_redirects=False,
        )
        assert response.code == 500
        assert CLIENT_SECRET not in response.body.decode()

    def test_unauthenticated_redirects_to_hub_login(self) -> None:
        """The callback requires the Hub session like any browser route."""
        response = self.fetch(
            f"{self.service_url('callback')}?code=auth-code-1&state=whatever",
            follow_redirects=False,
        )
        assert response.code == 302
        assert response.headers["Location"].startswith("/hub/api/oauth2/authorize")


class TestCallbackHygiene(ServiceTestCase):
    """No sensitive value ever reaches a log line or a response body."""

    def test_no_sensitive_value_in_logs_or_bodies(self) -> None:
        """A happy flow plus a hostile probe leak nothing, even at DEBUG."""
        with self.assertLogs(level="DEBUG") as captured:
            state = self.begin_login(self.cookie_headers())
            probe = self.fetch(
                f"{self.service_url('callback')}?code=stolen-code&state={state}",
                headers=self.cookie_headers(BOB_TOKEN),
                follow_redirects=False,
            )
            state2 = self.begin_login(self.cookie_headers())
            success = self.fetch(
                f"{self.service_url('callback')}?code=auth-code-1&state={state2}",
                headers=self.cookie_headers(),
                follow_redirects=False,
            )
        assert probe.code == 400
        assert success.code == 200
        logs = "\n".join(captured.output)
        assert "[SECURITY]" in logs
        for value in (state, state2, "auth-code-1", "stolen-code", CLIENT_SECRET, "rt-1", "at-1"):
            assert value not in logs
        for body in (probe.body.decode(), success.body.decode()):
            for value in (state, state2, CLIENT_SECRET, "rt-1", "at-1"):
                assert value not in body
