"""End-to-end suite against the live local bench (tier 3, opt-in).

Precondition: the bench is up (``cd infra && docker compose up -d &&
jupyterhub -f infra/jupyterhub_config.py``); otherwise every test is
skipped with that exact instruction. Run with ``pytest -m e2e`` (the
default run excludes the marker). The suite drives the same HTTP a
browser would: the Hub login form, the Hub-to-service OAuth handshake,
the Keycloak login form, and the tessera callback, with cookie jars
only, no JavaScript.

Real sleeps, on purpose: the bench realms issue 60-second access tokens
precisely so expiry, refresh, and rotation can be exercised for real,
and Keycloak's clock cannot be mocked from here. This is the documented
exception to the mocked-clock rule of tiers 1 and 2. Expect roughly
4 to 5 minutes of wall clock.

Future extensions, deliberately not implemented because they are
destructive for the shared bench: provider-down mid-flow (compose stop)
and IdP recycling (compose down/up while tokens are stored).
"""

from __future__ import annotations

import time

import pytest
from bench import SERVICE_PREFIX, HubSession, digest

pytestmark = pytest.mark.e2e

_TOKEN_TTL_SLEEP = 65.0  # just past the realm's 60 s access-token lifespan


def test_s1_full_flow_stable_discovery(hub_session: HubSession) -> None:
    """The full browser flow turns the stable-discovery status green.

    The realm grants an offline token for this server, so the enriched
    status must report the offline kind with no refresh expiry (Keycloak
    answers ``refresh_expires_in: 0`` for offline tokens).
    """
    initial = hub_session.status_json("stable-discovery")
    assert initial["has_token"] is False
    assert initial["refresh_kind"] == "unknown"
    page = hub_session.complete_flow("stable-discovery", "alice")
    assert "The token was stored" in page
    status = hub_session.status_json("stable-discovery")
    assert status["has_token"] is True
    assert status["expires_at"] is not None
    assert "offline_access" in status["scope"]
    assert status["refresh_kind"] == "offline"
    assert status["refresh_expires_at"] is None


def test_s2_full_flow_stable_explicit(hub_session: HubSession) -> None:
    """The explicit-endpoints server (no offline_access) works the same way.

    The refresh token is session-bound here, so the enriched status must
    report the session kind with the real dated refresh expiry.
    """
    assert hub_session.status_json("stable-explicit")["has_token"] is False
    page = hub_session.complete_flow("stable-explicit", "alice")
    assert "The token was stored" in page
    status = hub_session.status_json("stable-explicit")
    assert status["has_token"] is True
    assert status["expires_at"] is not None
    assert "offline_access" not in status["scope"]
    assert status["refresh_kind"] == "session"
    assert status["refresh_expires_at"] > time.time()


def test_s3_rotation_survives_consecutive_refreshes(hub_session: HubSession) -> None:
    """Three refreshes across expiry prove the rotated token is persisted.

    The rotating realm revokes a refresh token on use (max reuse 0): the
    third call can only succeed if the second call's rotated refresh
    token was stored, otherwise reuse detection would have killed it.
    """
    hub_session.complete_flow("rotating", "alice")
    api_token = hub_session.api_token()
    fingerprints = []
    for wait in (0.0, _TOKEN_TTL_SLEEP, _TOKEN_TTL_SLEEP):
        if wait:
            time.sleep(wait)
        response = hub_session.token_endpoint("rotating", api_token)
        assert response.status_code == 200
        fingerprints.append(digest(response.json()["access_token"]))
    assert len(set(fingerprints)) == 3


def test_s4_transparent_refresh_across_expiry(hub_session: HubSession) -> None:
    """A stored token yields a fresh access token after the old one expired."""
    hub_session.complete_flow("stable-discovery", "alice")
    api_token = hub_session.api_token()
    first = hub_session.token_endpoint("stable-discovery", api_token)
    assert first.status_code == 200
    time.sleep(_TOKEN_TTL_SLEEP)
    second = hub_session.token_endpoint("stable-discovery", api_token)
    assert second.status_code == 200
    assert digest(first.json()["access_token"]) != digest(second.json()["access_token"])
    assert second.json()["expires_at"] > first.json()["expires_at"]


def test_s5_per_user_isolation(hub_session: HubSession, second_session: HubSession) -> None:
    """Each Hub user sees only their own status and token."""
    hub_session.complete_flow("stable-discovery", "alice")
    assert second_session.status_json("stable-discovery")["has_token"] is False
    assert hub_session.status_json("stable-discovery")["has_token"] is True
    second_session.complete_flow("stable-discovery", "bob")
    assert second_session.status_json("stable-discovery")["has_token"] is True
    first_token = hub_session.token_endpoint("stable-discovery", hub_session.api_token())
    second_token = second_session.token_endpoint("stable-discovery", second_session.api_token())
    assert first_token.status_code == 200
    assert second_token.status_code == 200
    first_payload = first_token.json()
    second_payload = second_token.json()
    assert digest(first_payload["access_token"]) != digest(second_payload["access_token"])


def test_s6_cookie_session_never_reaches_a_token(hub_session: HubSession) -> None:
    """The token endpoint refuses the browser session outright."""
    hub_session.complete_flow("stable-discovery", "alice")
    response = hub_session.client.get(f"{SERVICE_PREFIX}/token?server=stable-discovery")
    assert response.status_code == 403
    assert "access_token" not in response.text


def test_s7a_unknown_server_is_404(hub_session: HubSession) -> None:
    """An undeclared server name gets the JSON 404."""
    api_token = hub_session.api_token()
    response = hub_session.client.get(
        f"{SERVICE_PREFIX}/status?server=nowhere",
        headers={"Authorization": f"token {api_token}"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "unknown_server"


def test_s7b_no_stored_token_is_409(hub_session: HubSession) -> None:
    """Asking for a token before any sign-in yields the actionable 409."""
    response = hub_session.token_endpoint("stable-discovery", hub_session.api_token())
    assert response.status_code == 409
    assert response.json()["error"] == "no_stored_token"


def test_s7c_forged_state_is_static_400(hub_session: HubSession) -> None:
    """A callback with a never-issued state gets the static page, no echo."""
    hub_session.status_json("stable-discovery")  # walks the service handshake
    response = hub_session.follow(f"{SERVICE_PREFIX}/callback?code=x&state=e2e-forged-state")
    assert response.status_code == 400
    assert "e2e-forged-state" not in response.text


def test_s8_provider_rejection_shows_returned_error_page(hub_session: HubSession) -> None:
    """A bogus code with a valid state hits the real IdP and gets the new page."""
    state = hub_session.authorization_state("stable-discovery")
    response = hub_session.follow(f"{SERVICE_PREFIX}/callback?code=bogus-e2e-code&state={state}")
    assert response.status_code == 502
    assert "returned an error" in response.text
    assert "could not be reached" not in response.text
    assert "bogus-e2e-code" not in response.text


def test_s9_verify_reports_real_validity(hub_session: HubSession) -> None:
    """Verify answers true on a live stored token, over the real cookie+XSRF path.

    This is also the live proof of the browser POST path: the session
    carries the Hub OAuth cookie and the service-scoped ``_xsrf`` cookie
    from the real handshake, echoed as ``X-XSRFToken``.
    """
    hub_session.complete_flow("stable-discovery", "alice")
    response = hub_session.post_action("verify", "stable-discovery")
    assert response.status_code == 200
    assert response.json() == {"valid": True}


def test_s10_revoke_notifies_keycloak_and_turns_status_red(hub_session: HubSession) -> None:
    """Revoke notifies the real IdP (discovery realm), purges, and stays honest.

    The discovery realm declares a revocation endpoint, so the provider
    must be genuinely notified; the second revoke proves the purge with
    the actionable 409.
    """
    hub_session.complete_flow("stable-discovery", "alice")
    response = hub_session.post_action("revoke", "stable-discovery")
    assert response.status_code == 200
    assert response.json() == {"revoked": True, "provider_notified": True}
    assert hub_session.status_json("stable-discovery")["has_token"] is False
    second = hub_session.post_action("revoke", "stable-discovery")
    assert second.status_code == 409
    assert second.json()["error"] == "no_stored_token"
