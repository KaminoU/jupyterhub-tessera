"""Tests for the deployment-wide service configuration (ServiceConfig)."""

from __future__ import annotations

import pytest

from tessera.config.errors import ConfigValidationError
from tessera.config.models import ServiceConfig


class TestServiceConfig:
    """Validation of the top-level service section."""

    def test_valid_public_url(self) -> None:
        """A service with an https public_url validates."""
        service = ServiceConfig.from_mapping({"public_url": "https://hub.example.com"})
        assert service.public_url == "https://hub.example.com"

    def test_redirect_uri_is_derived_and_fixed(self) -> None:
        """The redirect_uri derives from public_url with the fixed service path."""
        service = ServiceConfig.from_mapping({"public_url": "https://hub.example.com"})
        assert service.redirect_uri == "https://hub.example.com/services/tessera/callback"

    def test_trailing_slash_normalized(self) -> None:
        """A trailing slash on public_url never doubles in the redirect_uri."""
        service = ServiceConfig.from_mapping({"public_url": "https://hub.example.com/"})
        assert service.public_url == "https://hub.example.com"
        assert service.redirect_uri == "https://hub.example.com/services/tessera/callback"

    def test_base_path_preserved(self) -> None:
        """A Hub served under a base path keeps it in the redirect_uri."""
        service = ServiceConfig.from_mapping({"public_url": "https://hub.example.com/jupyter"})
        assert service.redirect_uri == "https://hub.example.com/jupyter/services/tessera/callback"

    def test_loopback_http_accepted(self) -> None:
        """Plain http is accepted for a loopback host (local development)."""
        service = ServiceConfig.from_mapping({"public_url": "http://127.0.0.1:8000"})
        assert service.redirect_uri == "http://127.0.0.1:8000/services/tessera/callback"

    def test_not_mapping_rejected(self) -> None:
        """A non-mapping service value is rejected."""
        with pytest.raises(ConfigValidationError):
            ServiceConfig.from_mapping("nope")

    def test_public_url_missing_rejected(self) -> None:
        """A missing public_url is rejected."""
        with pytest.raises(ConfigValidationError):
            ServiceConfig.from_mapping({})

    def test_public_url_blank_rejected(self) -> None:
        """A blank public_url is rejected."""
        with pytest.raises(ConfigValidationError):
            ServiceConfig.from_mapping({"public_url": "   "})

    def test_public_url_non_string_rejected(self) -> None:
        """A non-string public_url is rejected."""
        with pytest.raises(ConfigValidationError):
            ServiceConfig.from_mapping({"public_url": 8443})

    def test_http_non_loopback_rejected(self) -> None:
        """Plain http on a non-loopback host is rejected."""
        with pytest.raises(ConfigValidationError):
            ServiceConfig.from_mapping({"public_url": "http://hub.example.com"})

    def test_url_without_host_rejected(self) -> None:
        """A URL without a host is rejected."""
        with pytest.raises(ConfigValidationError):
            ServiceConfig.from_mapping({"public_url": "https://"})

    def test_query_rejected(self) -> None:
        """A public_url carrying a query string is rejected."""
        with pytest.raises(ConfigValidationError):
            ServiceConfig.from_mapping({"public_url": "https://hub.example.com?x=1"})

    def test_fragment_rejected(self) -> None:
        """A public_url carrying a fragment is rejected."""
        with pytest.raises(ConfigValidationError):
            ServiceConfig.from_mapping({"public_url": "https://hub.example.com#frag"})
