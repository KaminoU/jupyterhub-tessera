"""HTTP tests for ``/token``: token-only auth, contract, single-flight.

Rejections first: the route structurally refuses cookies (its plain
``HubAuth`` cannot read them), refuses anonymous and non-user callers,
and maps every flow outcome to the JSON contract. The single-flight
test proves the coalescing guarantee across the HTTP layer with two
genuinely concurrent requests and zero sleeps.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading

from jupyterhub.services.auth import HubAuth
from kstlib.auth.errors import TokenRefreshError
from support import (
    ALICE_TOKEN,
    LOWPRIV_TOKEN,
    PREFIX,
    SECRET_ENV_NAME,
    SERVICE_KIND_TOKEN,
    ServiceTestCase,
)
from tornado.testing import gen_test
from tornado.web import Application

from tessera.flow.orchestrator import FlowOrchestrator
from tessera.service.handlers import TokenHandler


class TestTokenHappyPath(ServiceTestCase):
    """A stored refresh token yields a fresh access token as JSON."""

    def test_returns_fresh_access_token(self) -> None:
        """The JSON carries the refreshed token, uncacheable."""
        self.seed_token()
        response = self.fetch(
            self.service_url("token", server="manual-idp"), headers=self.token_headers()
        )
        assert response.code == 200
        assert response.headers["Cache-Control"] == "no-store"
        payload = json.loads(response.body)
        assert payload == {"access_token": "at-fresh", "expires_at": None, "scope": "openid"}
        assert len(self.journal.refreshes) == 1


class TestTokenAuthMatrix(ServiceTestCase):
    """Only a token-authenticated Hub user reaches a token."""

    def test_cookie_alone_never_reaches_a_token(self) -> None:
        """A valid browser session without a token is structurally refused."""
        self.seed_token()
        response = self.fetch(
            self.service_url("token", server="manual-idp"), headers=self.cookie_headers()
        )
        assert response.code == 403
        assert "at-fresh" not in response.body.decode()
        assert self.journal.refreshes == []

    def test_unauthenticated_is_403(self) -> None:
        """No credentials at all: 403, not a login redirect."""
        response = self.fetch(self.service_url("token", server="manual-idp"))
        assert response.code == 403
        assert json.loads(response.body)["error"] == "http_error"

    def test_insufficient_scopes_rejected(self) -> None:
        """A user without the service access scope gets 403."""
        response = self.fetch(
            self.service_url("token", server="manual-idp"),
            headers=self.token_headers(LOWPRIV_TOKEN),
        )
        assert response.code == 403

    def test_service_identity_rejected(self) -> None:
        """A service identity is refused even with the access scope."""
        response = self.fetch(
            self.service_url("token", server="manual-idp"),
            headers=self.token_headers(SERVICE_KIND_TOKEN),
        )
        assert response.code == 403


class TestTokenContract(ServiceTestCase):
    """Every flow outcome maps to the documented JSON contract."""

    def test_no_stored_token_is_409_actionable(self) -> None:
        """Without a stored token the client learns to use the button."""
        response = self.fetch(
            self.service_url("token", server="manual-idp"), headers=self.token_headers()
        )
        assert response.code == 409
        payload = json.loads(response.body)
        assert payload["error"] == "no_stored_token"
        assert "button" in payload["detail"]

    def test_rejected_refresh_is_409_and_invalidates(self) -> None:
        """A definitive rejection reports 409 and turns the status red."""
        self.journal.refresh_result = TokenRefreshError("invalid_grant", retryable=False)
        self.seed_token()
        response = self.fetch(
            self.service_url("token", server="manual-idp"), headers=self.token_headers()
        )
        assert response.code == 409
        assert json.loads(response.body)["error"] == "refresh_rejected"
        status = self.fetch(
            self.service_url("status", server="manual-idp"), headers=self.token_headers()
        )
        assert json.loads(status.body)["has_token"] is False

    def test_transient_refresh_failure_is_502(self) -> None:
        """A retryable provider failure is a gateway error; the record stays."""
        self.journal.refresh_result = TokenRefreshError("gateway timeout", retryable=True)
        self.seed_token()
        response = self.fetch(
            self.service_url("token", server="manual-idp"), headers=self.token_headers()
        )
        assert response.code == 502
        assert json.loads(response.body)["error"] == "refresh_unavailable"
        status = self.fetch(
            self.service_url("status", server="manual-idp"), headers=self.token_headers()
        )
        payload = json.loads(status.body)
        assert payload["has_token"] is True
        assert payload["expires_at"] is None

    def test_unknown_server_is_404_json(self) -> None:
        """An undeclared server name gets a JSON 404."""
        response = self.fetch(
            self.service_url("token", server="nowhere"), headers=self.token_headers()
        )
        assert response.code == 404
        assert json.loads(response.body)["error"] == "unknown_server"

    def test_missing_server_is_400_json(self) -> None:
        """The server parameter is mandatory, as JSON."""
        response = self.fetch(self.service_url("token"), headers=self.token_headers())
        assert response.code == 400
        assert json.loads(response.body)["error"] == "http_error"

    def test_unresolvable_secret_is_500_json(self) -> None:
        """A missing client secret is a deployment error, as JSON."""
        self.seed_token()
        os.environ.pop(SECRET_ENV_NAME, None)
        response = self.fetch(
            self.service_url("token", server="manual-idp"), headers=self.token_headers()
        )
        assert response.code == 500
        assert json.loads(response.body)["error"] == "service_misconfigured"

    def test_hygiene_stored_token_never_leaks(self) -> None:
        """The stored refresh token value never reaches logs or bodies."""
        self.journal.refresh_result = TokenRefreshError("invalid_grant", retryable=False)
        self.seed_token(refresh_token="rt-super-confidential")
        with self.assertLogs(level="DEBUG") as captured:
            response = self.fetch(
                self.service_url("token", server="manual-idp"), headers=self.token_headers()
            )
        assert response.code == 409
        logs = "\n".join(captured.output)
        assert "[SECURITY]" in logs
        assert "rt-super-confidential" not in logs
        assert "rt-super-confidential" not in response.body.decode()


class _InstrumentedTokenHandler(TokenHandler):
    """TokenHandler counting entries so a test can gate deterministically."""

    def initialize(  # type: ignore[override]  # reason: tornado wires initialize from URLSpec kwargs (Callable[..., None])
        self,
        orchestrator: FlowOrchestrator,
        hub_auth: HubAuth,
        entries: list[int],
        both_in: asyncio.Event,
    ) -> None:
        """Receive the base collaborators plus the test instrumentation.

        Args:
            orchestrator: The process-wide flow orchestrator.
            hub_auth: The token-only Hub authenticator.
            entries: The shared entry counter.
            both_in: Set when the second request has entered the handler.
        """
        super().initialize(orchestrator, hub_auth)
        self._entries = entries
        self._both_in = both_in

    async def get(self) -> None:
        """Count the entry, signal the second one, then run the real route."""
        self._entries.append(1)
        if len(self._entries) == 2:
            self._both_in.set()
        # @web.authenticated types its wrapper as Optional[Awaitable[None]].
        pending = super().get()
        if pending is not None:
            await pending


class TestTokenSingleFlight(ServiceTestCase):
    """Two concurrent requests for one pair share a single refresh."""

    def build_app(self) -> Application:
        """Serve only the instrumented token route for this test."""
        self.entries: list[int] = []
        self.both_in = asyncio.Event()
        self.release = threading.Event()
        self.journal.refresh_gate = self.release
        return Application(
            [
                (
                    f"{PREFIX}token",
                    _InstrumentedTokenHandler,
                    {
                        "orchestrator": self.orchestrator,
                        "hub_auth": self.hub_auth,
                        "entries": self.entries,
                        "both_in": self.both_in,
                    },
                ),
            ],
            cookie_secret=self.cookie_secret,
            xsrf_cookies=True,
        )

    @gen_test(timeout=15)
    async def test_concurrent_requests_share_one_refresh(self) -> None:
        """Both callers get the same fresh token from one provider call.

        Deterministic and sleep-free: the provider blocks on a gate that
        the test opens only once both requests have entered the handler;
        after its entry count, the second handler reaches the coalescing
        point synchronously, so it always joins the in-flight refresh.
        """
        await self.store.save("alice", "manual-idp", "rt-stored")
        url = self.get_url(self.service_url("token", server="manual-idp"))
        first = self.http_client.fetch(url, headers=self.token_headers(ALICE_TOKEN))
        second = self.http_client.fetch(url, headers=self.token_headers(ALICE_TOKEN))
        await self.both_in.wait()
        self.release.set()
        responses = await asyncio.gather(first, second)
        bodies = [json.loads(response.body) for response in responses]
        assert [response.code for response in responses] == [200, 200]
        assert bodies[0] == bodies[1]
        assert bodies[0]["access_token"] == "at-fresh"
        assert len(self.journal.refreshes) == 1
