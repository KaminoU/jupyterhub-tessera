"""Tests for the OAuth flow crypto primitives (state and PKCE S256)."""

from __future__ import annotations

import base64
import hashlib

from tessera.flow import compute_code_challenge, generate_code_verifier, generate_state

# RFC 7636 Appendix B known-answer vector (verified with hashlib).
_RFC7636_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
_RFC7636_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

_UNRESERVED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


class TestState:
    """The anti-CSRF state is unpredictable and carries strong entropy."""

    def test_state_is_unique_per_call(self) -> None:
        """Two states differ (crypto randomness, not a constant)."""
        assert generate_state() != generate_state()

    def test_state_has_strong_entropy(self) -> None:
        """A state is long enough to carry 256 bits of entropy."""
        assert len(generate_state()) >= 43

    def test_state_uses_unreserved_charset(self) -> None:
        """A state uses only URL-safe unreserved characters."""
        assert set(generate_state()) <= _UNRESERVED


class TestCodeVerifier:
    """The PKCE code verifier respects the RFC 7636 shape."""

    def test_verifier_length_within_rfc7636_range(self) -> None:
        """A verifier length is in the RFC 7636 [43, 128] range."""
        verifier = generate_code_verifier()
        assert 43 <= len(verifier) <= 128

    def test_verifier_uses_unreserved_charset(self) -> None:
        """A verifier uses only URL-safe unreserved characters."""
        assert set(generate_code_verifier()) <= _UNRESERVED

    def test_verifier_is_unique_per_call(self) -> None:
        """Two verifiers differ."""
        assert generate_code_verifier() != generate_code_verifier()


class TestCodeChallenge:
    """The S256 code challenge is the unpadded base64url of the verifier hash."""

    def test_challenge_matches_manual_s256(self) -> None:
        """The challenge equals base64url(sha256(verifier)) without padding."""
        verifier = generate_code_verifier()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        assert compute_code_challenge(verifier) == expected

    def test_challenge_has_no_padding(self) -> None:
        """The challenge carries no base64 padding characters."""
        assert "=" not in compute_code_challenge(generate_code_verifier())

    def test_challenge_matches_rfc7636_vector(self) -> None:
        """The S256 transform matches the RFC 7636 Appendix B known answer."""
        assert compute_code_challenge(_RFC7636_VERIFIER) == _RFC7636_CHALLENGE
