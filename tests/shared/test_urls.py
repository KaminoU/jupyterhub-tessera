"""Tests for the shared URL acceptance rule.

The rule is reached from two directions in production: the configuration
layer, where the operator writes the value, and the OAuth flow, where the
value comes from a provider's discovery document. These tests exercise it
directly, through its public API only.
"""

from __future__ import annotations

from urllib.parse import SplitResult

import pytest

from tessera._shared import urls
from tessera._shared.urls import (
    MAX_URL_LENGTH,
    REASON_BAD_HOST,
    REASON_BAD_PORT,
    REASON_INSECURE_SCHEME,
    REASON_NOT_A_STRING,
    REASON_NOT_A_URL,
    REASON_NOT_PRINTABLE,
    REASON_TOO_LONG,
    REASON_USERINFO,
    UrlValidationError,
    validate_url,
)

# A host of exactly 254 characters that every label rule accepts: only the
# total length can reject it, so it is the one value that covers that branch.
_HOST_254 = ".".join(["a" * 63] * 3) + "." + "a" * 62
# The same shape one character shorter, which must be accepted.
_HOST_253 = ".".join(["a" * 63] * 3) + "." + "a" * 61
# A URL of exactly MAX_URL_LENGTH characters, and one character too long.
_URL_AT_LIMIT = "https://a.example/" + "b" * (MAX_URL_LENGTH - 18)
_URL_OVER_LIMIT = "https://a.example/" + "b" * (MAX_URL_LENGTH - 17)

_REJECTED: list[tuple[str, str, str]] = [
    # The value that reached production: a shell variable the paste escaped.
    ("field-incident", "https://$\\{SRV\\}:9999", REASON_BAD_HOST),
    ("unexpanded-variable", "https://${SRV}:9999", REASON_BAD_HOST),
    ("template-placeholder", "https://{{host}}/x", REASON_BAD_HOST),
    ("space-in-host", "https://my host.example", REASON_NOT_PRINTABLE),
    ("backslash-in-host", "https://host\\example", REASON_BAD_HOST),
    ("quote-in-host", "https://host'x.example", REASON_BAD_HOST),
    ("non-numeric-port", "https://host.example:99a9", REASON_BAD_PORT),
    ("port-out-of-range", "https://host.example:99999/x", REASON_BAD_PORT),
    # The parser drops these three bytes, so the bound has to run before it.
    ("carriage-return", "https://hub.example.org\r\n.evil", REASON_NOT_PRINTABLE),
    ("tab", "https://hub.exa\tmple.org", REASON_NOT_PRINTABLE),
    ("fullwidth-number-sign", "https://host\uff03.example/x", REASON_NOT_PRINTABLE),
    # The parser lowercases the host, which maps this one to an ASCII 'k'.
    ("kelvin-sign", "https://\u212a.example.com/x", REASON_NOT_PRINTABLE),
    ("internationalized-host", "https://b\u00fccher.example/x", REASON_NOT_PRINTABLE),
    ("userinfo", "https://idp.example.com@evil.example/authorize", REASON_USERINFO),
    ("userinfo-with-port", "https://idp.example.com:443@evil.example/a", REASON_USERINFO),
    ("empty-userinfo", "https://@evil.example/", REASON_USERINFO),
    # Percent-encoding the separator does not smuggle it past the host rule.
    ("encoded-at-sign", "https://idp.example.com%40evil.example/x", REASON_BAD_HOST),
    ("bare-dot-host", "https://./x", REASON_BAD_HOST),
    ("dashes-only-host", "https://---/x", REASON_BAD_HOST),
    ("leading-dash-label", "https://-evil.example/x", REASON_BAD_HOST),
    ("trailing-dash-label", "https://a-.example/x", REASON_BAD_HOST),
    ("empty-label", "https://a..b.example/x", REASON_BAD_HOST),
    ("oversized-label", "https://" + "a" * 254 + "/x", REASON_BAD_HOST),
    ("oversized-host", f"https://{_HOST_254}/x", REASON_BAD_HOST),
    ("oversized-url", _URL_OVER_LIMIT, REASON_TOO_LONG),
    # Rules that predate this test file, kept so nothing silently drops them.
    ("empty-string", "   ", REASON_NOT_A_STRING),
    ("no-host", "https://", REASON_NOT_A_URL),
    ("no-scheme", "/protocol/openid-connect/auth", REASON_NOT_A_URL),
    ("wrong-scheme", "ftp://idp.example.com/x", REASON_NOT_A_URL),
    ("http-off-loopback", "http://idp.example.com/authorize", REASON_INSECURE_SCHEME),
]

_ACCEPTED: list[tuple[str, str]] = [
    ("https-endpoint", "https://idp.example.com/authorize"),
    ("https-with-port", "https://hub.example.org:9999"),
    ("uppercase-host", "https://HOST.EXAMPLE.ORG"),
    ("dash-in-label", "https://host-with-dash.example"),
    ("underscore-in-label", "https://host_with_underscore.example"),
    ("punycode-host", "https://xn--bcher-kva.example/x"),
    ("absolute-fqdn", "https://idp.example.com./x"),
    ("host-at-limit", f"https://{_HOST_253}"),
    ("url-at-limit", _URL_AT_LIMIT),
    ("http-localhost", "http://localhost:8000"),
    ("http-ipv4-loopback", "http://127.0.0.1:8000"),
    ("http-ipv6-loopback", "http://[::1]:8000"),
    # The bracket guard must leave a legitimate IPv6 authority alone.
    ("http-ipv6-loopback-with-path", "http://[::1]:8000/x"),
]


@pytest.mark.parametrize(
    ("value", "reason"),
    [pytest.param(value, reason, id=name) for name, value, reason in _REJECTED],
)
def test_unacceptable_url_is_rejected_without_echoing_it(value: str, reason: str) -> None:
    """Every unacceptable URL raises with its own reason and never quotes the value."""
    with pytest.raises(UrlValidationError) as caught:
        validate_url(value)
    assert caught.value.reason == reason
    assert value not in caught.value.detail
    assert value not in str(caught.value)


@pytest.mark.parametrize("value", [pytest.param(value, id=name) for name, value in _ACCEPTED])
def test_acceptable_url_is_returned_unchanged(value: str) -> None:
    """Every acceptable URL is returned as it was given."""
    assert validate_url(value) == value


def test_surrounding_whitespace_is_stripped() -> None:
    """A value padded with whitespace is accepted and returned stripped."""
    assert validate_url("  https://idp.example.com/authorize  ") == (
        "https://idp.example.com/authorize"
    )


@pytest.mark.parametrize("value", [None, 123, b"https://idp.example.com"])
def test_non_string_is_rejected(value: object) -> None:
    """A value that is not a string is rejected before any parsing."""
    with pytest.raises(UrlValidationError) as caught:
        validate_url(value)
    assert caught.value.reason == REASON_NOT_A_STRING


@pytest.mark.parametrize(
    "value",
    ["https://[::1/x", "https://[127.0.0.1]/x"],
    ids=["unbalanced-bracket", "ipv4-in-brackets"],
)
def test_bracketed_host_raises_the_validators_own_error(value: str) -> None:
    """A malformed bracketed host raises this module's error, not the parser's.

    The reason code is deliberately not asserted: the parser rejects these at
    different stages depending on the interpreter patch level, and what the
    caller relies on is the error type and the absence of the value.
    """
    with pytest.raises(UrlValidationError) as caught:
        validate_url(value)
    assert "[" not in caught.value.detail
    assert value not in caught.value.detail


@pytest.mark.parametrize(
    ("netloc", "host"),
    [
        pytest.param("[127.0.0.1]", "127.0.0.1", id="ipv4-in-brackets"),
        pytest.param("[not-an-ip]", "not-an-ip", id="name-in-brackets"),
        pytest.param("[]", None, id="empty-brackets"),
    ],
)
def test_bracketed_non_ipv6_host_is_rejected_even_when_the_parser_allows_it(
    monkeypatch: pytest.MonkeyPatch, netloc: str, host: str | None
) -> None:
    """A bracketed authority that carries anything but an IPv6 literal is refused.

    Interpreters before 3.10.13 hand such an authority over without checking
    it, so the rule has to hold on its own rather than inherit a parser
    behaviour. The stand-in is the very ``SplitResult`` those interpreters
    return, not a stub: the assertion below shows its host is computed by the
    parser's own property.
    """
    parts = SplitResult("https", netloc, "/x", "", "")
    assert parts.hostname == host

    def _split_like_an_old_parser(url: str) -> SplitResult:
        return parts

    monkeypatch.setattr(urls, "urlsplit", _split_like_an_old_parser)
    with pytest.raises(UrlValidationError) as caught:
        validate_url(f"https://{netloc}/x")
    assert caught.value.reason == REASON_BAD_HOST
