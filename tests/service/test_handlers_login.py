"""HTTP tests for ``/login``: auth matrix, hardening, IdP redirect.

Rejections first: the endpoint refuses anonymous, under-scoped, and
non-user identities, hardens its ``server`` parameter, and maps every
flow-layer failure to its contract status. The happy path asserts the
exact redirect the browser receives.
"""

from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlsplit

from support import (
    AUTHORIZE_URL,
    CLIENT_SECRET,
    LOWPRIV_TOKEN,
    PUBLIC_URL,
    SECRET_ENV_NAME,
    SERVICE_KIND_TOKEN,
    FailingDiscoveryProvider,
    ServiceTestCase,
)

from tessera.config.models import ServerConfig


class TestLoginHappyPath(ServiceTestCase):
    """An authenticated login replies 302 to the identity provider."""

    def test_token_auth_redirects_to_idp(self) -> None:
        """An API token starts the flow and gets the full IdP redirect."""
        response = self.fetch(
            self.service_url("login", server="manual-idp"),
            headers=self.token_headers(),
            follow_redirects=False,
        )
        assert response.code == 302
        location = response.headers["Location"]
        assert location.startswith(f"{AUTHORIZE_URL}?")
        params = dict(parse_qsl(urlsplit(location).query))
        assert params["response_type"] == "code"
        assert params["client_id"] == "tessera-demo-client"
        assert params["redirect_uri"] == f"{PUBLIC_URL}/services/tessera/callback"
        assert params["code_challenge_method"] == "S256"
        assert params["state"]
        assert params["code_challenge"]
        assert CLIENT_SECRET not in location

    def test_cookie_auth_redirects_to_idp(self) -> None:
        """A browser session (Hub OAuth cookie) starts the flow the same way."""
        response = self.fetch(
            self.service_url("login", server="manual-idp"),
            headers=self.cookie_headers(),
            follow_redirects=False,
        )
        assert response.code == 302
        assert response.headers["Location"].startswith(f"{AUTHORIZE_URL}?")


class TestLoginRejections(ServiceTestCase):
    """Every unauthorized or malformed login is refused."""

    def test_unauthenticated_redirects_to_hub_login(self) -> None:
        """No credentials: the browser is sent to the Hub OAuth login."""
        response = self.fetch(
            self.service_url("login", server="manual-idp"), follow_redirects=False
        )
        assert response.code == 302
        assert response.headers["Location"].startswith("/hub/api/oauth2/authorize")

    def test_insufficient_scopes_rejected(self) -> None:
        """A user without the service access scope gets 403."""
        response = self.fetch(
            self.service_url("login", server="manual-idp"),
            headers=self.token_headers(LOWPRIV_TOKEN),
            follow_redirects=False,
        )
        assert response.code == 403

    def test_service_identity_rejected(self) -> None:
        """A service identity is refused even with the access scope."""
        response = self.fetch(
            self.service_url("login", server="manual-idp"),
            headers=self.token_headers(SERVICE_KIND_TOKEN),
            follow_redirects=False,
        )
        assert response.code == 403

    def test_missing_server_rejected(self) -> None:
        """The server parameter is mandatory."""
        response = self.fetch(
            self.service_url("login"), headers=self.token_headers(), follow_redirects=False
        )
        assert response.code == 400

    def test_oversized_server_rejected(self) -> None:
        """An oversized server name is refused and never echoed."""
        oversized = "x" * 300
        response = self.fetch(
            self.service_url("login", server=oversized),
            headers=self.token_headers(),
            follow_redirects=False,
        )
        assert response.code == 400
        assert oversized not in response.body.decode()

    def test_unknown_server_rejected(self) -> None:
        """An undeclared server name gets 404."""
        response = self.fetch(
            self.service_url("login", server="nowhere"),
            headers=self.token_headers(),
            follow_redirects=False,
        )
        assert response.code == 404

    def test_unresolvable_secret_is_500(self) -> None:
        """A missing client secret is a deployment error, not a user error."""
        os.environ.pop(SECRET_ENV_NAME, None)
        response = self.fetch(
            self.service_url("login", server="discovery-idp"),
            headers=self.token_headers(),
            follow_redirects=False,
        )
        assert response.code == 500
        assert CLIENT_SECRET not in response.body.decode()


class TestLoginPendingStoreFull(ServiceTestCase):
    """A saturated pending-flow store maps to 503."""

    pending_max_size = 0

    def test_full_store_is_503(self) -> None:
        """The login is refused with 503 when no flow slot is available."""
        response = self.fetch(
            self.service_url("login", server="manual-idp"),
            headers=self.token_headers(),
            follow_redirects=False,
        )
        assert response.code == 503


class TestLoginDiscoveryFailure(ServiceTestCase):
    """A failing OIDC discovery maps to 502."""

    def provider_factory(self, server: ServerConfig, secret: str) -> FailingDiscoveryProvider:
        """Hand back a provider whose discovery always fails."""
        return FailingDiscoveryProvider()

    def test_discovery_failure_is_502(self) -> None:
        """The provider being unreachable is a gateway error."""
        response = self.fetch(
            self.service_url("login", server="discovery-idp"),
            headers=self.token_headers(),
            follow_redirects=False,
        )
        assert response.code == 502
