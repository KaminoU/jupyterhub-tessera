"""Shared acceptance rule for URLs a browser may be sent to.

Both the configuration layer (provider endpoints and the service public URL,
written by the operator) and the OAuth flow (the authorization endpoint read
from a provider's discovery document) must decide whether a candidate URL is
safe to redirect a browser to. This module owns the single implementation so
a value coming from the network meets the same bar as one coming from the
configuration file: https is always accepted, http only for a loopback host
(local development, in the spirit of RFC 8252).

The value is bounded before it is parsed, by length and to printable ASCII.
The parser silently drops tab, carriage return and newline, and a character
it has already removed can no longer be examined, while the accepted string
still carries it. An internationalized host must therefore be given in its
punycode form (``xn--...``), which is ASCII.

Callers wrap :class:`UrlValidationError` into their own public error type.
The rejected value is never included in the error, so a caller can log the
reason without echoing a string it does not control. The parser's own
exceptions are turned into this error for the same reason: some of their
messages quote the value they refuse.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

MAX_URL_LENGTH = 2048
MAX_HOST_LENGTH = 253

REASON_NOT_A_STRING = "not_a_string"
REASON_TOO_LONG = "too_long"
REASON_NOT_PRINTABLE = "not_printable"
REASON_NOT_A_URL = "not_a_url"
REASON_USERINFO = "userinfo"
REASON_BAD_HOST = "bad_host"
REASON_BAD_PORT = "bad_port"
REASON_INSECURE_SCHEME = "insecure_scheme"

# A hostname label: letters, digits and underscore, dashes allowed inside
# only, 63 characters at most. The underscore is tolerated deliberately: it
# is not RFC 1123, and it is present on real hosts.
_LABEL = r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?"
_HOSTNAME = re.compile(rf"{_LABEL}(?:\.{_LABEL})*\.?")


class UrlValidationError(Exception):
    """A candidate value is not an acceptable URL.

    Attributes:
        reason: A stable code identifying what failed, one of
            :data:`REASON_NOT_A_STRING`, :data:`REASON_TOO_LONG`,
            :data:`REASON_NOT_PRINTABLE`, :data:`REASON_NOT_A_URL`,
            :data:`REASON_USERINFO`, :data:`REASON_BAD_HOST`,
            :data:`REASON_BAD_PORT`, or :data:`REASON_INSECURE_SCHEME`.
            Callers switch on it to phrase a diagnostic that fits their own
            domain.
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


def _check_bounds(url: str) -> None:
    """Bound a candidate value before anything parses it.

    The bound runs first on purpose: the parser silently drops tab, carriage
    return and newline, and a character it has already removed can no longer
    be examined.

    Args:
        url: The stripped candidate value.

    Raises:
        UrlValidationError: If the value is oversized, or carries a character
            outside printable ASCII.
    """
    if len(url) > MAX_URL_LENGTH:
        raise UrlValidationError(REASON_TOO_LONG, f"must be at most {MAX_URL_LENGTH} characters")
    if not all("\x21" <= char <= "\x7e" for char in url):
        raise UrlValidationError(
            REASON_NOT_PRINTABLE, "must contain only printable ASCII characters"
        )


def _is_ipv6_literal(host: str | None) -> bool:
    """Tell whether a parsed host is an IPv6 literal.

    Args:
        host: The host of an already parsed URL, or None when it has none.

    Returns:
        True only for an IPv6 address, which is the one host a bracketed
        authority may carry.
    """
    if host is None:
        return False
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address)
    except ValueError:
        return False


def _has_host_form(host: str) -> bool:
    """Tell whether a parsed host could be a host at all.

    Args:
        host: The host of an already parsed URL, lowercased by the parser.

    Returns:
        True for an IP literal, or for a dotted sequence of hostname labels
        no longer than :data:`MAX_HOST_LENGTH`.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return bool(_HOSTNAME.fullmatch(host)) and len(host.rstrip(".")) <= MAX_HOST_LENGTH
    return True


def validate_url(raw: object) -> str:
    """Validate that ``raw`` is an acceptable https (or loopback http) URL.

    The value is bounded before it is parsed, then its authority is checked:
    no userinfo, a host that is an IP literal or a hostname, and a port that
    is a number.

    Args:
        raw: The candidate value, from configuration or from the network.

    Returns:
        The stripped, validated URL string.

    Raises:
        UrlValidationError: If the value is not an acceptable URL. Neither
            the rejected value nor the parser's own message is part of the
            error.

    Examples:
        >>> validate_url("  https://idp.example.com/authorize  ")
        'https://idp.example.com/authorize'
    """
    if not isinstance(raw, str) or not raw.strip():
        raise UrlValidationError(REASON_NOT_A_STRING, "must be a non-empty URL string")
    url = raw.strip()
    _check_bounds(url)
    try:
        parts = urlsplit(url)
    except ValueError:
        # Not chained: some of these messages quote the netloc they refuse.
        raise UrlValidationError(REASON_NOT_A_URL, "must be an http(s) URL with a host") from None
    if "@" in parts.netloc:
        raise UrlValidationError(
            REASON_USERINFO, "must not carry a userinfo component in its authority"
        )
    # Brackets are for an IPv6 literal and nothing else. Recent parsers raise
    # on any other bracketed host, older ones hand it over unchecked: the
    # decision must not depend on the interpreter's patch level.
    if "[" in parts.netloc and not _is_ipv6_literal(parts.hostname):
        raise UrlValidationError(
            REASON_BAD_HOST, "must have an IP literal or a hostname as its host"
        )
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise UrlValidationError(REASON_NOT_A_URL, "must be an http(s) URL with a host")
    if not _has_host_form(parts.hostname):
        raise UrlValidationError(
            REASON_BAD_HOST, "must have an IP literal or a hostname as its host"
        )
    try:
        _ = parts.port
    except ValueError:
        # Not chained: the message quotes the port it refuses.
        raise UrlValidationError(REASON_BAD_PORT, "must carry a numeric port in range") from None
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        raise UrlValidationError(
            REASON_INSECURE_SCHEME,
            "must use https (http is allowed only for a loopback host)",
        )
    return url
