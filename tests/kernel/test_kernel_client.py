"""Tests for the kernel-side token client (``tessera.kernel``).

Every request goes over ``httpx.MockTransport`` (zero network). The suite
covers the happy path (``get_token`` and the ``TESSERA_TOKEN`` proxy), the
environment-driven URL cascade, the mandatory API token, every non-200
mapping and its token-free message, network failures, malformed answers,
the IPython load/unload hooks, and the hygiene guarantee that no token
value ever appears in an error message.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from tessera.kernel import (
    TESSERA_TOKEN,
    TesseraTokenError,
    get_token,
    load_ipython_extension,
    unload_ipython_extension,
)
from tessera.kernel import client as kernel_client

_API_TOKEN = "hub-api-token-sentinel-abc123"
_BASE_URL = "http://127.0.0.1:8000/services/tessera/"

_Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def api_only(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Set the API token and clear every service-URL variable."""
    monkeypatch.setenv("JUPYTERHUB_API_TOKEN", _API_TOKEN)
    monkeypatch.delenv("TESSERA_URL", raising=False)
    monkeypatch.delenv("JUPYTERHUB_PUBLIC_URL", raising=False)
    monkeypatch.delenv("JUPYTERHUB_BASE_URL", raising=False)
    return monkeypatch


@pytest.fixture
def env(api_only: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A ready environment: API token plus an explicit ``TESSERA_URL``."""
    api_only.setenv("TESSERA_URL", _BASE_URL)
    return api_only


def _install(monkeypatch: pytest.MonkeyPatch, handler: _Handler) -> None:
    """Route the client's HTTP through a mock transport (no network)."""
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(kernel_client, "_new_client", lambda: httpx.Client(transport=transport))


def _respond(
    status: int,
    *,
    json: object | None = None,
    text: str = "",
    capture: list[httpx.Request] | None = None,
) -> _Handler:
    """Return a handler answering with a fixed status and body."""

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        if json is not None:
            return httpx.Response(status, json=json)
        return httpx.Response(status, text=text)

    return handler


def _ok(token: str, capture: list[httpx.Request] | None = None) -> _Handler:
    """Return a handler answering 200 with the given access token."""
    return _respond(
        200,
        json={"access_token": token, "expires_at": None, "scope": "openid"},
        capture=capture,
    )


class _FakeShell:
    """Minimal IPython shell double exposing ``push`` and ``user_ns``."""

    def __init__(self) -> None:
        self.user_ns: dict[str, object] = {}

    def push(self, variables: dict[str, object]) -> None:
        """Merge ``variables`` into the namespace, like IPython does."""
        self.user_ns.update(variables)


def test_get_token_returns_fresh_value(env: pytest.MonkeyPatch) -> None:
    """A 200 answer yields the access token string it carries."""
    _install(env, _ok("at-fresh-123"))
    assert get_token("my-idp") == "at-fresh-123"


def test_proxy_subscript_returns_fresh_value(env: pytest.MonkeyPatch) -> None:
    """``TESSERA_TOKEN["srv"]`` returns the same fresh token value."""
    _install(env, _ok("at-proxy-456"))
    assert TESSERA_TOKEN["my-idp"] == "at-proxy-456"


def test_request_targets_the_token_endpoint_with_auth(
    env: pytest.MonkeyPatch,
) -> None:
    """The call hits ``.../token`` with the API token as a bearer header."""
    seen: list[httpx.Request] = []
    _install(env, _ok("at-1", capture=seen))
    get_token("my-idp")
    assert len(seen) == 1
    assert str(seen[0].url) == f"{_BASE_URL}token?server=my-idp"
    assert seen[0].headers["Authorization"] == f"token {_API_TOKEN}"


def test_server_name_is_url_encoded(env: pytest.MonkeyPatch) -> None:
    """A server name with query-breaking characters is percent-encoded."""
    seen: list[httpx.Request] = []
    _install(env, _ok("at-1", capture=seen))
    get_token("srv &weird=1")
    assert seen[0].url.params["server"] == "srv &weird=1"


def test_tessera_url_wins_and_keeps_trailing_slash(
    env: pytest.MonkeyPatch,
) -> None:
    """An explicit ``TESSERA_URL`` is used verbatim when already slashed."""
    seen: list[httpx.Request] = []
    _install(env, _ok("at-1", capture=seen))
    get_token("my-idp")
    assert str(seen[0].url).startswith(_BASE_URL)


def test_tessera_url_gets_a_trailing_slash(api_only: pytest.MonkeyPatch) -> None:
    """A ``TESSERA_URL`` without a trailing slash gets one appended."""
    api_only.setenv("TESSERA_URL", "http://host:9000/services/tessera")
    seen: list[httpx.Request] = []
    _install(api_only, _ok("at-1", capture=seen))
    get_token("my-idp")
    assert str(seen[0].url) == "http://host:9000/services/tessera/token?server=my-idp"


def test_service_url_derived_from_public_url(
    api_only: pytest.MonkeyPatch,
) -> None:
    """Without ``TESSERA_URL`` the base derives from the public URL."""
    api_only.setenv("JUPYTERHUB_PUBLIC_URL", "https://hub.example.com/user/alice/")
    seen: list[httpx.Request] = []
    _install(api_only, _ok("at-1", capture=seen))
    get_token("my-idp")
    assert str(seen[0].url).startswith("https://hub.example.com/services/tessera/token")


def test_service_url_honors_hub_base_path(
    api_only: pytest.MonkeyPatch,
) -> None:
    """A non-root ``JUPYTERHUB_BASE_URL`` is folded into the derived base."""
    api_only.setenv("JUPYTERHUB_PUBLIC_URL", "https://hub.example.com/user/alice/")
    api_only.setenv("JUPYTERHUB_BASE_URL", "/jupyter")
    seen: list[httpx.Request] = []
    _install(api_only, _ok("at-1", capture=seen))
    get_token("my-idp")
    assert str(seen[0].url).startswith("https://hub.example.com/jupyter/services/tessera/token")


def test_service_url_unresolvable_raises(api_only: pytest.MonkeyPatch) -> None:
    """With neither variable set, the client says how to fix it."""
    with pytest.raises(TesseraTokenError) as excinfo:
        get_token("my-idp")
    assert "TESSERA_URL" in str(excinfo.value)


def test_missing_api_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside a single-user server the missing token is reported."""
    monkeypatch.delenv("JUPYTERHUB_API_TOKEN", raising=False)
    monkeypatch.setenv("TESSERA_URL", _BASE_URL)
    with pytest.raises(TesseraTokenError) as excinfo:
        get_token("my-idp")
    assert "single-user server" in str(excinfo.value)


def test_error_uses_service_detail(env: pytest.MonkeyPatch) -> None:
    """A JSON error surfaces the service ``detail`` and the status."""
    _install(
        env,
        _respond(
            404,
            json={
                "error": "unknown_server",
                "detail": "This server is not declared in the tessera configuration.",
            },
        ),
    )
    with pytest.raises(TesseraTokenError) as excinfo:
        get_token("nowhere")
    message = str(excinfo.value)
    assert "not declared" in message
    assert "HTTP 404" in message


def test_error_detail_is_bounded(env: pytest.MonkeyPatch) -> None:
    """An over-long JSON detail is truncated in the raised message."""
    huge = "detail-" + "x" * 5000
    _install(env, _respond(500, json={"detail": huge}))
    with pytest.raises(TesseraTokenError) as excinfo:
        get_token("my-idp")
    message = str(excinfo.value)
    assert huge not in message
    assert "detail-xxxxx" in message
    assert len(message) < 400


def test_error_falls_back_to_error_slug(env: pytest.MonkeyPatch) -> None:
    """With no ``detail``, the ``error`` slug is used instead."""
    _install(env, _respond(409, json={"error": "refresh_rejected"}))
    with pytest.raises(TesseraTokenError) as excinfo:
        get_token("my-idp")
    assert "refresh_rejected" in str(excinfo.value)


def test_error_falls_back_to_body_text(env: pytest.MonkeyPatch) -> None:
    """A non-JSON error body is surfaced as a bounded slice."""
    _install(env, _respond(500, text="boom internal error"))
    with pytest.raises(TesseraTokenError) as excinfo:
        get_token("my-idp")
    assert "boom internal error" in str(excinfo.value)


def test_error_from_non_dict_json(env: pytest.MonkeyPatch) -> None:
    """A JSON body that is not an object still yields a message."""
    _install(env, _respond(500, json=["weird"]))
    with pytest.raises(TesseraTokenError) as excinfo:
        get_token("my-idp")
    assert "weird" in str(excinfo.value)


def test_error_default_message_on_empty_body(env: pytest.MonkeyPatch) -> None:
    """An empty, non-JSON error body gets a generic message."""
    _install(env, _respond(502, text=""))
    with pytest.raises(TesseraTokenError) as excinfo:
        get_token("my-idp")
    message = str(excinfo.value)
    assert "the tessera service returned an error" in message
    assert "HTTP 502" in message


def test_network_failure_raises(env: pytest.MonkeyPatch) -> None:
    """A transport error is reported as an unreachable service."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install(env, refuse)
    with pytest.raises(TesseraTokenError) as excinfo:
        get_token("my-idp")
    message = str(excinfo.value)
    assert "could not reach the tessera service" in message
    assert "ConnectError" in message


def test_unexpected_200_missing_field(env: pytest.MonkeyPatch) -> None:
    """A 200 without ``access_token`` is an unexpected response."""
    _install(env, _respond(200, json={"foo": "bar"}))
    with pytest.raises(TesseraTokenError) as excinfo:
        get_token("my-idp")
    assert "unexpected response" in str(excinfo.value)


def test_unexpected_200_non_json(env: pytest.MonkeyPatch) -> None:
    """A 200 with a non-JSON body is an unexpected response."""
    _install(env, _respond(200, text="not json at all"))
    with pytest.raises(TesseraTokenError) as excinfo:
        get_token("my-idp")
    assert "unexpected response" in str(excinfo.value)


def test_proxy_propagates_error(env: pytest.MonkeyPatch) -> None:
    """The proxy raises the same error the function would."""
    _install(env, _respond(409, json={"error": "no_stored_token"}))
    with pytest.raises(TesseraTokenError):
        _ = TESSERA_TOKEN["my-idp"]


def test_proxy_repr_hints_without_token() -> None:
    """The proxy repr is a usage hint, never a token value."""
    assert repr(TESSERA_TOKEN) == '<tessera tokens: use TESSERA_TOKEN["<server>"]>'


def test_load_and_unload_roundtrip() -> None:
    """The hooks inject both names and then remove them cleanly."""
    shell = _FakeShell()
    load_ipython_extension(shell)
    assert shell.user_ns["TESSERA_TOKEN"] is TESSERA_TOKEN
    assert shell.user_ns["get_token"] is get_token
    unload_ipython_extension(shell)
    assert "TESSERA_TOKEN" not in shell.user_ns
    assert "get_token" not in shell.user_ns


def test_unload_is_idempotent() -> None:
    """Unloading a shell that never loaded the names does not raise."""
    shell = _FakeShell()
    unload_ipython_extension(shell)
    assert shell.user_ns == {}


def test_error_message_never_contains_the_api_token(
    env: pytest.MonkeyPatch,
) -> None:
    """A non-200 message carries the API token nowhere."""
    _install(env, _respond(409, json={"error": "no_stored_token"}))
    with pytest.raises(TesseraTokenError) as excinfo:
        get_token("my-idp")
    assert _API_TOKEN not in str(excinfo.value)


def test_network_error_never_contains_the_api_token(
    env: pytest.MonkeyPatch,
) -> None:
    """A transport-error message carries the API token nowhere."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused for {_API_TOKEN}")

    _install(env, refuse)
    with pytest.raises(TesseraTokenError) as excinfo:
        get_token("my-idp")
    assert _API_TOKEN not in str(excinfo.value)


def test_client_factory_builds_a_timed_client() -> None:
    """The default client factory returns a timed httpx client (no I/O)."""
    with kernel_client._new_client() as client:
        assert isinstance(client, httpx.Client)
        assert client.timeout.read == pytest.approx(10.0)
