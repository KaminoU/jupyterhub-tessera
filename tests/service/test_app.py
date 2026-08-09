"""Tests for the application factory: routing, settings, prefix handling."""

from __future__ import annotations

import json

from support import PREFIX, ServiceTestCase
from tornado.web import Application

from tessera.service.app import make_app


class TestAppWiring(ServiceTestCase):
    """The factory wires the routes and the hardening settings."""

    def test_unknown_route_under_prefix_is_404(self) -> None:
        """No catch-all exists under the prefix."""
        response = self.fetch(f"{PREFIX}nope", headers=self.token_headers())
        assert response.code == 404

    def test_route_outside_prefix_is_404(self) -> None:
        """The bare route names are not served outside the prefix."""
        response = self.fetch("/login", headers=self.token_headers())
        assert response.code == 404

    def test_hub_oauth_callback_route_is_registered(self) -> None:
        """The Hub handshake route exists (upstream rejects a bare call)."""
        response = self.fetch(f"{PREFIX}oauth_callback", follow_redirects=False)
        assert response.code == 400  # upstream: callback without a code

    def test_xsrf_protection_and_cookie_secret_are_set(self) -> None:
        """The factory enables XSRF and installs the cookie secret."""
        app = self.build_app()
        assert app.settings["xsrf_cookies"] is True
        assert app.settings["cookie_secret"] == self.cookie_secret


class TestPrefixNormalization(ServiceTestCase):
    """A prefix without surrounding slashes is normalized, not broken."""

    def build_app(self) -> Application:
        """Build the app with a deliberately unnormalized prefix."""
        return make_app(
            self.orchestrator,
            prefix="services/tessera",
            hub_auth=self.hub_auth,
            hub_oauth=self.hub_oauth,
            cookie_secret=self.cookie_secret,
        )

    def test_routes_are_served_under_the_normalized_prefix(self) -> None:
        """The status route answers under the slash-wrapped prefix."""
        response = self.fetch(
            self.service_url("status", server="manual-idp"), headers=self.token_headers()
        )
        assert response.code == 200
        assert json.loads(response.body)["has_token"] is False
