"""Cryptographic primitives for the OAuth authorization-code flow.

Generates the anti-CSRF ``state`` and the PKCE (RFC 7636) code verifier and
its S256 code challenge. All randomness comes from :mod:`secrets` (never
:mod:`random`), so the values are unpredictable and safe against guessing.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

_STATE_NBYTES = 32
_VERIFIER_NBYTES = 64


def generate_state() -> str:
    """Return a fresh, unpredictable anti-CSRF state token.

    Returns:
        A URL-safe random string carrying 256 bits of entropy.
    """
    return secrets.token_urlsafe(_STATE_NBYTES)


def generate_code_verifier() -> str:
    """Return a fresh PKCE code verifier.

    Returns:
        A URL-safe, high-entropy string within the RFC 7636 length range of
        43 to 128 characters.
    """
    return secrets.token_urlsafe(_VERIFIER_NBYTES)


def compute_code_challenge(verifier: str) -> str:
    """Derive the PKCE S256 code challenge from a code verifier.

    Args:
        verifier: The PKCE code verifier.

    Returns:
        The base64url-encoded SHA-256 of the verifier without padding, which
        is the ``S256`` transformation defined by RFC 7636.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
