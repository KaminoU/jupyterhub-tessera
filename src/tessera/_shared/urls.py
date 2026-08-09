"""Shared acceptance rule for URLs a browser may be sent to.

Both the configuration layer (provider endpoints and the service public URL,
written by the operator) and the OAuth flow (the authorization endpoint read
from a provider's discovery document) must decide whether a candidate URL is
safe to redirect a browser to. This module owns the single implementation so
a value coming from the network meets the same bar as one coming from the
configuration file: https is always accepted, http only for a loopback host
(local development, in the spirit of RFC 8252).

Callers wrap :class:`UrlValidationError` into their own public error type.
The rejected value is never included in the error, so a caller can log the
reason without echoing a string it does not control.
"""

from __future__ import annotations

from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

REASON_NOT_A_STRING = "not_a_string"
REASON_NOT_A_URL = "not_a_url"
REASON_INSECURE_SCHEME = "insecure_scheme"


class UrlValidationError(Exception):
    """A candidate value is not an acceptable URL.

    Attributes:
        reason: A stable code identifying what failed, one of
            :data:`REASON_NOT_A_STRING`, :data:`REASON_NOT_A_URL`, or
            :data:`REASON_INSECURE_SCHEME`. Callers switch on it to phrase a
            diagnostic that fits their own domain.
        detail: A human-readable clause describing the expectation, meant to
            be appended after a caller-owned subject (for example
            ``"server 'demo': 'provider.issuer' "``).
    """

    def __init__(self, reason: str, detail: str) -> None:
        """Build a validation error carrying both a code and a clause.

        Args:
            reason: The stable failure code.
            detail: The human-readable expectation clause.
        """
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def validate_url(raw: object) -> str:
    """Validate that ``raw`` is an acceptable https (or loopback http) URL.

    Args:
        raw: The candidate value, from configuration or from the network.

    Returns:
        The stripped, validated URL string.

    Raises:
        UrlValidationError: If the value is not an acceptable URL. The
            rejected value is not part of the error.

    Examples:
        >>> validate_url("  https://idp.example.com/authorize  ")
        'https://idp.example.com/authorize'
    """
    if not isinstance(raw, str) or not raw.strip():
        raise UrlValidationError(REASON_NOT_A_STRING, "must be a non-empty URL string")
    url = raw.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise UrlValidationError(REASON_NOT_A_URL, "must be an http(s) URL with a host")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        raise UrlValidationError(
            REASON_INSECURE_SCHEME,
            "must use https (http is allowed only for a loopback host)",
        )
    return url
