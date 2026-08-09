"""HTTP tests for ``/servers``: the static button topology the panel loads.

Rejection-first: the route refuses exactly what ``/status`` refuses
(anonymous poller redirected to the Hub login, missing scope 403,
service identity 403). The payload mirrors the validated configuration,
in declaration order, and never carries a token or a secret.
"""

from __future__ import annotations

import json

from support import LOWPRIV_TOKEN, SERVICE_KIND_TOKEN, ServiceTestCase

EXPECTED_SERVERS = {
    "servers": [
        {
            "name": "manual-idp",
            "label": "manual-idp",
            "color_valid": "#2e7d32",
            "color_invalid": "#c62828",
        },
        {
            "name": "discovery-idp",
            "label": "discovery-idp",
            "color_valid": "#2e7d32",
            "color_invalid": "#c62828",
        },
    ]
}


class TestServersRejections(ServiceTestCase):
    """The servers route refuses what every other route refuses."""

    def test_unauthenticated_redirects_to_hub_login(self) -> None:
        """No credentials: the caller is sent to the Hub OAuth login."""
        response = self.fetch(self.service_url("servers"), follow_redirects=False)
        assert response.code == 302
        assert response.headers["Location"].startswith("/hub/api/oauth2/authorize")

    def test_insufficient_scopes_rejected(self) -> None:
        """A user without the service access scope gets 403."""
        response = self.fetch(
            self.service_url("servers"), headers=self.token_headers(LOWPRIV_TOKEN)
        )
        assert response.code == 403

    def test_service_identity_rejected(self) -> None:
        """A non-user identity is refused like on every other route."""
        response = self.fetch(
            self.service_url("servers"), headers=self.token_headers(SERVICE_KIND_TOKEN)
        )
        assert response.code == 403


class TestServersContract(ServiceTestCase):
    """The servers payload mirrors the configuration, in declaration order."""

    def test_declared_servers_in_declaration_order(self) -> None:
        """The exact declared topology is returned, nothing more."""
        response = self.fetch(self.service_url("servers"), headers=self.token_headers())
        assert response.code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert json.loads(response.body) == EXPECTED_SERVERS

    def test_cookie_auth_works_for_the_panel(self) -> None:
        """A browser session lists the servers like a token does."""
        response = self.fetch(self.service_url("servers"), headers=self.cookie_headers())
        assert response.code == 200
        assert json.loads(response.body) == EXPECTED_SERVERS

    def test_no_secret_material_in_the_payload(self) -> None:
        """Client ids, secrets, and endpoints never reach the panel."""
        body = self.fetch(self.service_url("servers"), headers=self.token_headers()).body.decode(
            "utf-8"
        )
        for needle in ("client_id", "client_secret", "issuer", "token_endpoint", "https://"):
            assert needle not in body
