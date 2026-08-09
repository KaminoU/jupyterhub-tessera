"""Tests for the OAuth flow orchestrator (flow.orchestrator).

Security-heavy and rejection-first: a callback is accepted only with a
one-shot, unexpired state bound to the same user; every rejection logs a
[SECURITY] line that never carries the state, the code, the verifier, a
token, or the client secret. The kstlib pitfalls are pinned by regression
tests running the real providers over ``httpx.MockTransport`` (zero
network): a legitimate flow must stay [SECURITY]-silent and the exchange
must carry tessera's PKCE verifier to the discovered token endpoint.
``get_access_token`` is covered the same way: rejections first (no stored
token, definitive rejection invalidating the record, transient failure
keeping it), then rotation semantics, then the single-flight guarantee
under concurrency. The executor boundary is pinned too: blocking kstlib
work goes through the injected executor, and the one accepted secret-cache
race resolves twice to the same value. The clock is injected everywhere;
no test sleeps.
"""

from __future__ import annotations

import asyncio
import io
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from kstlib.auth import (
    AuthProviderConfig,
    MemoryTokenStorage,
    OAuth2Provider,
    OIDCProvider,
    Token,
)
from kstlib.auth.errors import TokenExchangeError, TokenRefreshError, TokenValidationError

import tessera.flow.orchestrator as orchestrator_module
from tessera.config.models import ServerConfig, ServiceConfig, StoreConfig, TesseraConfig
from tessera.flow import (
    CallbackValidationError,
    ExchangeError,
    ExchangeRejectedError,
    FlowOrchestrator,
    NoStoredTokenError,
    PendingFlow,
    PendingFlowStore,
    PendingFlowStoreFull,
    RefreshRejectedError,
    SecretResolutionError,
    UnknownServerError,
    compute_code_challenge,
    generate_code_verifier,
    generate_state,
)
from tessera.flow.secrets import resolve_client_secret
from tessera.service.runner import configure_logging
from tessera.store import TokenRecord, TokenStore

_SECRET = "a-very-confidential-client-secret-value"
_ENV_NAME = "TESSERA_TEST_ORCH_SECRET"
_PUBLIC_URL = "https://hub.example.com"
_REDIRECT_URI = f"{_PUBLIC_URL}/services/tessera/callback"
_ISSUER = "https://idp.example.com/realms/demo"
_AUTHORIZE_URL = "https://idp.example.com/authorize"
_TOKEN_URL = "https://idp.example.com/token"
_DISCOVERED_AUTHORIZE = f"{_ISSUER}/protocol/openid-connect/auth"
_DISCOVERED_TOKEN = f"{_ISSUER}/protocol/openid-connect/token"


class _Clock:
    """A controllable clock returning a fixed value until moved."""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _tessera_config(tmp_path: Path) -> TesseraConfig:
    """Build a validated two-server config (explicit + discovery modes)."""
    key_path = tmp_path / "db.key"
    key_path.write_text("a-strong-test-passphrase-for-the-store", encoding="utf-8")
    key_path.chmod(0o600)
    servers = {
        "manual-idp": ServerConfig.from_mapping(
            "manual-idp",
            {
                "provider": {
                    "authorization_endpoint": _AUTHORIZE_URL,
                    "token_endpoint": _TOKEN_URL,
                },
                "client_id": "tessera-demo-client",
                "client_secret_env": _ENV_NAME,
                "scopes": ["openid", "profile"],
            },
        ),
        "discovery-idp": ServerConfig.from_mapping(
            "discovery-idp",
            {
                "provider": {"issuer": _ISSUER},
                "client_id": "tessera-demo-client",
                "client_secret_env": _ENV_NAME,
                "scopes": ["openid"],
            },
        ),
    }
    return TesseraConfig(
        servers=servers,
        store=StoreConfig.from_mapping(
            {"db_location": str(tmp_path / "tokens.db"), "key_file": str(key_path)}
        ),
        service=ServiceConfig.from_mapping({"public_url": _PUBLIC_URL}),
    )


def _orchestrator(
    config: TesseraConfig,
    tokens: TokenStore,
    *,
    factory: orchestrator_module.ProviderFactory | None = None,
    clock: _Clock | None = None,
    max_size: int = 100,
    executor: ThreadPoolExecutor | None = None,
) -> tuple[FlowOrchestrator, PendingFlowStore, _Clock]:
    """Assemble an orchestrator with a shared fake clock."""
    clock = clock or _Clock()
    pending = PendingFlowStore(max_size=max_size, clock=clock)
    orch = FlowOrchestrator(
        config, pending, tokens, provider_factory=factory, clock=clock, executor=executor
    )
    return orch, pending, clock


def _url_param(url: str, name: str) -> str:
    """Return a single query parameter from a URL."""
    return dict(parse_qsl(urlsplit(url).query))[name]


def _token(
    *,
    refresh: str | None = "rt-1",
    expires_at: datetime | None = None,
    scope: list[str] | None = None,
    metadata: dict[str, object] | None = None,
    id_token: str | None = None,
) -> Token:
    """Build a kstlib token as returned by an exchange."""
    return Token(
        access_token="at-1",
        expires_at=expires_at,
        refresh_token=refresh,
        scope=scope or [],
        metadata=dict(metadata or {}),
        id_token=id_token,
    )


class _RecordingProvider(OAuth2Provider):
    """A no-network provider recording the stateless exchanges it receives."""

    def __init__(self, result: Token | Exception) -> None:
        super().__init__(
            "stub",
            AuthProviderConfig(
                client_id="tessera-demo-client",
                authorize_url=_AUTHORIZE_URL,
                token_url=_TOKEN_URL,
                pkce=False,
            ),
            MemoryTokenStorage(),
        )
        self.result = result
        self.exchanges: list[tuple[str, str | None]] = []
        self.closed = False

    def exchange_code_stateless(self, code: str, *, code_verifier: str | None = None) -> Token:
        """Record the exchange arguments and return (or raise) the result."""
        self.exchanges.append((code, code_verifier))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self) -> None:
        """Record closure and delegate."""
        self.closed = True
        super().close()


def _forbidden_factory(server: ServerConfig, secret: str) -> OAuth2Provider:
    """Fail the test if the orchestrator reaches the provider at all."""
    pytest.fail("provider factory must not be called for a rejected callback")


class _TrackingOIDCProvider(OIDCProvider):
    """An OIDC provider counting close() calls through its public surface."""

    def __init__(
        self,
        name: str,
        config: AuthProviderConfig,
        token_storage: MemoryTokenStorage,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        super().__init__(name, config, token_storage, http_client=http_client)
        self.close_calls = 0

    def close(self) -> None:
        """Count and delegate."""
        self.close_calls += 1
        super().close()


def _discovery_handler(requests_seen: list[httpx.Request]) -> httpx.MockTransport:
    """A transport serving discovery and token responses, recording requests."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": _ISSUER,
                    "authorization_endpoint": _DISCOVERED_AUTHORIZE,
                    "token_endpoint": _DISCOVERED_TOKEN,
                    "jwks_uri": f"{_ISSUER}/protocol/openid-connect/certs",
                },
            )
        return httpx.Response(
            200,
            json={
                "access_token": "at-1",
                "refresh_token": "rt-1",
                "expires_in": 3600,
                "scope": "openid",
            },
        )

    return httpx.MockTransport(handler)


class TestBeginLogin:
    """begin_login builds tessera's own URL and registers the flow last."""

    async def test_explicit_mode_builds_exact_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The authorization URL carries exactly the expected parameters."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        orch, pending, clock = _orchestrator(config, TokenStore(config.store))
        url = await orch.begin_login("alice", "manual-idp")
        base, _, query = url.partition("?")
        assert base == _AUTHORIZE_URL
        params = dict(parse_qsl(query))
        assert params == {
            "response_type": "code",
            "client_id": "tessera-demo-client",
            "redirect_uri": _REDIRECT_URI,
            "scope": "openid profile",
            "state": params["state"],
            "code_challenge": params["code_challenge"],
            "code_challenge_method": "S256",
        }
        flow = pending.consume(params["state"])
        assert flow is not None
        assert flow.username == "alice"
        assert flow.provider == "manual-idp"
        assert flow.created_at == clock.now
        assert compute_code_challenge(flow.code_verifier) == params["code_challenge"]

    async def test_unknown_server_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An undeclared server name raises UnknownServerError."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        orch, _, _ = _orchestrator(config, TokenStore(config.store))
        with pytest.raises(UnknownServerError):
            await orch.begin_login("alice", "nowhere")

    async def test_discovery_mode_uses_discovered_endpoint_and_closes_provider(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Discovery resolves the real authorization endpoint; provider is closed."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        requests_seen: list[httpx.Request] = []
        created: list[_TrackingOIDCProvider] = []

        def factory(server: ServerConfig, secret: str) -> OAuth2Provider:
            provider = _TrackingOIDCProvider(
                server.name,
                AuthProviderConfig(
                    client_id=server.client_id,
                    client_secret=secret,
                    issuer=_ISSUER,
                    scopes=list(server.scopes),
                    redirect_uri=_REDIRECT_URI,
                    pkce=False,
                ),
                MemoryTokenStorage(),
                http_client=httpx.Client(transport=_discovery_handler(requests_seen)),
            )
            created.append(provider)
            return provider

        orch, _, _ = _orchestrator(config, TokenStore(config.store), factory=factory)
        url = await orch.begin_login("alice", "discovery-idp")
        assert url.startswith(f"{_DISCOVERED_AUTHORIZE}?")
        assert _url_param(url, "scope") == "openid"
        assert created[0].close_calls == 1

    async def test_discovery_failure_wraps_and_leaves_no_orphan_flow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed discovery raises ExchangeError and registers nothing."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        created: list[_TrackingOIDCProvider] = []

        def factory(server: ServerConfig, secret: str) -> OAuth2Provider:
            provider = _TrackingOIDCProvider(
                server.name,
                AuthProviderConfig(
                    client_id=server.client_id,
                    client_secret=secret,
                    issuer=_ISSUER,
                    pkce=False,
                ),
                MemoryTokenStorage(),
                http_client=httpx.Client(
                    transport=httpx.MockTransport(lambda request: httpx.Response(500))
                ),
            )
            created.append(provider)
            return provider

        orch, _, _ = _orchestrator(config, TokenStore(config.store), factory=factory, max_size=1)
        with pytest.raises(ExchangeError):
            await orch.begin_login("alice", "discovery-idp")
        assert created[0].close_calls == 1
        # The single slot is still free: the failed login left no orphan.
        assert (await orch.begin_login("alice", "manual-idp")).startswith(_AUTHORIZE_URL)

    @pytest.mark.parametrize(
        "authorization_endpoint",
        [None, "", 123],
        ids=["absent", "empty", "ill-typed"],
    )
    async def test_discovery_document_without_usable_authorization_endpoint_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        authorization_endpoint: object,
    ) -> None:
        """A completed discovery serving no usable endpoint raises, actionably.

        Silently using an issuer-derived placeholder would send the user to
        a provider 404; the operator must get the server name and what to
        check instead.
        """
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        created: list[_TrackingOIDCProvider] = []
        document: dict[str, object] = {
            "issuer": _ISSUER,
            "token_endpoint": _DISCOVERED_TOKEN,
            "jwks_uri": f"{_ISSUER}/protocol/openid-connect/certs",
        }
        if authorization_endpoint is not None:
            document["authorization_endpoint"] = authorization_endpoint

        def factory(server: ServerConfig, secret: str) -> OAuth2Provider:
            provider = _TrackingOIDCProvider(
                server.name,
                AuthProviderConfig(
                    client_id=server.client_id,
                    client_secret=secret,
                    issuer=_ISSUER,
                    pkce=False,
                ),
                MemoryTokenStorage(),
                http_client=httpx.Client(
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(200, json=document)
                    )
                ),
            )
            created.append(provider)
            return provider

        orch, _, _ = _orchestrator(config, TokenStore(config.store), factory=factory)
        with (
            caplog.at_level(logging.WARNING),
            pytest.raises(ExchangeError, match="server 'discovery-idp'.*authorization_endpoint"),
        ):
            await orch.begin_login("alice", "discovery-idp")
        assert created[0].close_calls == 1
        assert "returned no usable authorization_endpoint" in caplog.text

    @pytest.mark.parametrize(
        ("authorization_endpoint", "expected_log"),
        [
            (
                "http://idp.example.com/authorize",
                "announced a non-TLS authorization_endpoint",
            ),
            (
                "/protocol/openid-connect/auth",
                "announced an unusable authorization_endpoint",
            ),
        ],
        ids=["non-tls", "not-a-url"],
    )
    async def test_discovery_document_with_malformed_endpoint_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        authorization_endpoint: str,
        expected_log: str,
    ) -> None:
        """A discovered endpoint that is not an acceptable URL is refused.

        The value comes from the network, so it meets the same bar as a URL
        written by the operator: https, or http on a loopback host. The
        rejection names its nature so the operator knows whether the document
        is malformed or the provider advertises a non-TLS endpoint, without
        the log ever echoing the received value.
        """
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        created: list[_TrackingOIDCProvider] = []
        document = {
            "issuer": _ISSUER,
            "authorization_endpoint": authorization_endpoint,
            "token_endpoint": _DISCOVERED_TOKEN,
            "jwks_uri": f"{_ISSUER}/protocol/openid-connect/certs",
        }

        def factory(server: ServerConfig, secret: str) -> OAuth2Provider:
            provider = _TrackingOIDCProvider(
                server.name,
                AuthProviderConfig(
                    client_id=server.client_id,
                    client_secret=secret,
                    issuer=_ISSUER,
                    pkce=False,
                ),
                MemoryTokenStorage(),
                http_client=httpx.Client(
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(200, json=document)
                    )
                ),
            )
            created.append(provider)
            return provider

        orch, _, _ = _orchestrator(config, TokenStore(config.store), factory=factory)
        with (
            caplog.at_level(logging.WARNING),
            pytest.raises(ExchangeError, match="server 'discovery-idp'.*authorization_endpoint"),
        ):
            await orch.begin_login("alice", "discovery-idp")
        assert created[0].close_calls == 1
        assert expected_log in caplog.text
        assert authorization_endpoint not in caplog.text

    async def test_discovered_loopback_http_endpoint_is_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A loopback http endpoint stays usable, as the local bench serves one."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        loopback = "http://localhost:8008/realms/demo/protocol/openid-connect/auth"
        document = {
            "issuer": _ISSUER,
            "authorization_endpoint": loopback,
            "token_endpoint": _DISCOVERED_TOKEN,
            "jwks_uri": f"{_ISSUER}/protocol/openid-connect/certs",
        }

        def factory(server: ServerConfig, secret: str) -> OAuth2Provider:
            return _TrackingOIDCProvider(
                server.name,
                AuthProviderConfig(
                    client_id=server.client_id,
                    client_secret=secret,
                    issuer=_ISSUER,
                    pkce=False,
                ),
                MemoryTokenStorage(),
                http_client=httpx.Client(
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(200, json=document)
                    )
                ),
            )

        config = _tessera_config(tmp_path)
        orch, _, _ = _orchestrator(config, TokenStore(config.store), factory=factory)
        url = await orch.begin_login("alice", "discovery-idp")
        assert url.startswith(f"{loopback}?")

    async def test_authorization_endpoint_keeps_its_own_query(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An endpoint carrying a query keeps it alongside the flow parameters.

        RFC 6749 section 3.1 allows the authorization endpoint to include a
        query component, which the client must retain: a multi-tenant provider
        publishing `.../authorize?tenant=acme` must stay reachable.
        """
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        endpoint = f"{_DISCOVERED_AUTHORIZE}?tenant=acme"
        document = {
            "issuer": _ISSUER,
            "authorization_endpoint": endpoint,
            "token_endpoint": _DISCOVERED_TOKEN,
            "jwks_uri": f"{_ISSUER}/protocol/openid-connect/certs",
        }

        def factory(server: ServerConfig, secret: str) -> OAuth2Provider:
            return _TrackingOIDCProvider(
                server.name,
                AuthProviderConfig(
                    client_id=server.client_id,
                    client_secret=secret,
                    issuer=_ISSUER,
                    pkce=False,
                ),
                MemoryTokenStorage(),
                http_client=httpx.Client(
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(200, json=document)
                    )
                ),
            )

        config = _tessera_config(tmp_path)
        orch, _, _ = _orchestrator(config, TokenStore(config.store), factory=factory)
        url = await orch.begin_login("alice", "discovery-idp")
        assert url.count("?") == 1
        assert url.startswith(f"{_DISCOVERED_AUTHORIZE}?")
        assert _url_param(url, "tenant") == "acme"
        assert _url_param(url, "response_type") == "code"
        assert _url_param(url, "code_challenge_method") == "S256"

    async def test_discovery_server_with_pre_resolved_provider_skips_discovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A factory may hand back a provider with endpoints already resolved."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        stub = _RecordingProvider(_token())
        orch, _, _ = _orchestrator(
            config, TokenStore(config.store), factory=lambda server, secret: stub
        )
        url = await orch.begin_login("alice", "discovery-idp")
        assert url.startswith(f"{_AUTHORIZE_URL}?")
        assert stub.closed is True

    async def test_pending_store_full_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A saturated pending store bubbles up PendingFlowStoreFull."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        orch, _, _ = _orchestrator(config, TokenStore(config.store), max_size=0)
        with pytest.raises(PendingFlowStoreFull):
            await orch.begin_login("alice", "manual-idp")

    async def test_unresolvable_secret_rejected_before_discovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Discovery mode fails fast when the client secret cannot be resolved."""
        monkeypatch.delenv(_ENV_NAME, raising=False)
        config = _tessera_config(tmp_path)
        orch, _, _ = _orchestrator(config, TokenStore(config.store), factory=_forbidden_factory)
        with pytest.raises(SecretResolutionError):
            await orch.begin_login("alice", "discovery-idp")


class TestCallbackRejections:
    """Every invalid callback is refused with a sanitized [SECURITY] log."""

    async def test_unknown_state_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A state that was never registered is refused and never logged."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        orch, _, _ = _orchestrator(config, TokenStore(config.store), factory=_forbidden_factory)
        state = generate_state()
        with caplog.at_level(logging.DEBUG), pytest.raises(CallbackValidationError):
            await orch.complete_callback("alice", state, "auth-code-1")
        assert "[SECURITY]" in caplog.text
        assert state not in caplog.text

    async def test_replayed_state_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A state is one-shot: the second callback with it is refused."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        stub = _RecordingProvider(_token())
        async with TokenStore(config.store) as tokens:
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: stub)
            url = await orch.begin_login("alice", "manual-idp")
            state = _url_param(url, "state")
            await orch.complete_callback("alice", state, "auth-code-1")
            with pytest.raises(CallbackValidationError):
                await orch.complete_callback("alice", state, "auth-code-1")

    async def test_expired_state_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A state past the pending TTL is refused (clock mocked, no sleep)."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        orch, _, clock = _orchestrator(config, TokenStore(config.store), factory=_forbidden_factory)
        url = await orch.begin_login("alice", "manual-idp")
        state = _url_param(url, "state")
        clock.now += 600.0
        with caplog.at_level(logging.DEBUG), pytest.raises(CallbackValidationError):
            await orch.complete_callback("alice", state, "auth-code-1")
        assert "[SECURITY]" in caplog.text
        assert state not in caplog.text

    async def test_state_of_another_user_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A state started by alice is never accepted from bob."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        orch, _, _ = _orchestrator(config, TokenStore(config.store), factory=_forbidden_factory)
        url = await orch.begin_login("alice", "manual-idp")
        state = _url_param(url, "state")
        with caplog.at_level(logging.DEBUG), pytest.raises(CallbackValidationError):
            await orch.complete_callback("bob", state, "auth-code-1")
        assert "[SECURITY]" in caplog.text
        assert state not in caplog.text

    @pytest.mark.parametrize("state", ["", "x" * 4096])
    async def test_missing_or_oversized_state_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
    ) -> None:
        """An empty or oversized state is refused before any lookup."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        orch, _, _ = _orchestrator(config, TokenStore(config.store), factory=_forbidden_factory)
        with pytest.raises(CallbackValidationError):
            await orch.complete_callback("alice", state, "auth-code-1")

    @pytest.mark.parametrize("code", ["", "x" * 8192])
    async def test_missing_or_oversized_code_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
    ) -> None:
        """An empty or oversized authorization code is refused."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        orch, _, _ = _orchestrator(config, TokenStore(config.store), factory=_forbidden_factory)
        with pytest.raises(CallbackValidationError):
            await orch.complete_callback("alice", generate_state(), code)

    async def test_provider_rejection_at_exchange_is_distinguished(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A provider answering an OAuth error maps to ExchangeRejectedError.

        Real kstlib provider over MockTransport: the token endpoint
        responds 400 with an OAuth error body, so the rejection is
        definitive; nothing is stored and no provider value is logged.
        """
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                400,
                json={
                    "error": "not_allowed",
                    "error_description": "Offline tokens not allowed",
                },
            )
        )

        def factory(server: ServerConfig, secret: str) -> OAuth2Provider:
            return OAuth2Provider(
                server.name,
                AuthProviderConfig(
                    client_id=server.client_id,
                    client_secret=secret,
                    authorize_url=_AUTHORIZE_URL,
                    token_url=_TOKEN_URL,
                    pkce=False,
                ),
                MemoryTokenStorage(),
                http_client=httpx.Client(transport=transport),
            )

        async with TokenStore(config.store) as tokens:
            orch, _, _ = _orchestrator(config, tokens, factory=factory)
            url = await orch.begin_login("alice", "manual-idp")
            caplog.clear()
            with caplog.at_level(logging.DEBUG), pytest.raises(ExchangeRejectedError):
                await orch.complete_callback("alice", _url_param(url, "state"), "auth-code-1")
            assert await tokens.exists("alice", "manual-idp") is False
        assert "not_allowed" not in caplog.text
        assert "Offline tokens" not in caplog.text

    async def test_transport_failure_at_exchange_stays_exchange_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A network failure at the exchange stays a plain ExchangeError."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)

        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        def factory(server: ServerConfig, secret: str) -> OAuth2Provider:
            return OAuth2Provider(
                server.name,
                AuthProviderConfig(
                    client_id=server.client_id,
                    client_secret=secret,
                    authorize_url=_AUTHORIZE_URL,
                    token_url=_TOKEN_URL,
                    pkce=False,
                ),
                MemoryTokenStorage(),
                http_client=httpx.Client(transport=httpx.MockTransport(refuse)),
            )

        async with TokenStore(config.store) as tokens:
            orch, _, _ = _orchestrator(config, tokens, factory=factory)
            url = await orch.begin_login("alice", "manual-idp")
            with pytest.raises(ExchangeError):
                await orch.complete_callback("alice", _url_param(url, "state"), "auth-code-1")
            assert await tokens.exists("alice", "manual-idp") is False

    async def test_provider_5xx_at_exchange_is_transient(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A provider 5xx at the exchange is transient, not a rejection.

        Real kstlib provider over MockTransport: the token endpoint
        answers 500, so retrying the same exchange may succeed
        (``retryable`` in the kstlib contract). This must surface as a
        plain ExchangeError (the "could not be completed" page), never as
        the definitive ExchangeRejectedError.
        """
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        transport = httpx.MockTransport(lambda request: httpx.Response(500, text="internal error"))

        def factory(server: ServerConfig, secret: str) -> OAuth2Provider:
            return OAuth2Provider(
                server.name,
                AuthProviderConfig(
                    client_id=server.client_id,
                    client_secret=secret,
                    authorize_url=_AUTHORIZE_URL,
                    token_url=_TOKEN_URL,
                    pkce=False,
                ),
                MemoryTokenStorage(),
                http_client=httpx.Client(transport=transport),
            )

        async with TokenStore(config.store) as tokens:
            orch, _, _ = _orchestrator(config, tokens, factory=factory)
            url = await orch.begin_login("alice", "manual-idp")
            with pytest.raises(ExchangeError) as excinfo:
                await orch.complete_callback("alice", _url_param(url, "state"), "auth-code-1")
            assert not isinstance(excinfo.value, ExchangeRejectedError)
            assert await tokens.exists("alice", "manual-idp") is False

    async def test_failed_exchange_wraps_and_stores_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A kstlib exchange failure becomes ExchangeError; no token is stored."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        stub = _RecordingProvider(TokenExchangeError("boom"))
        async with TokenStore(config.store) as tokens:
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: stub)
            url = await orch.begin_login("alice", "manual-idp")
            with pytest.raises(ExchangeError):
                await orch.complete_callback("alice", _url_param(url, "state"), "auth-code-1")
            assert stub.closed is True
            assert await tokens.exists("alice", "manual-idp") is False

    async def test_non_exchange_auth_error_falls_back_to_exchange_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-exchange AuthError (invalid id_token) becomes a transient ExchangeError."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        stub = _RecordingProvider(TokenValidationError("invalid id_token"))
        async with TokenStore(config.store) as tokens:
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: stub)
            url = await orch.begin_login("alice", "manual-idp")
            with pytest.raises(ExchangeError) as excinfo:
                await orch.complete_callback("alice", _url_param(url, "state"), "auth-code-1")
            assert not isinstance(excinfo.value, ExchangeRejectedError)
            assert stub.closed is True
            assert await tokens.exists("alice", "manual-idp") is False

    async def test_response_without_refresh_token_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A token response without a refresh token is refused; nothing stored."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        stub = _RecordingProvider(_token(refresh=None))
        async with TokenStore(config.store) as tokens:
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: stub)
            url = await orch.begin_login("alice", "manual-idp")
            with pytest.raises(ExchangeError):
                await orch.complete_callback("alice", _url_param(url, "state"), "auth-code-1")
            assert await tokens.exists("alice", "manual-idp") is False


class TestCallbackHappyPath:
    """A valid callback exchanges the code and persists the refresh token."""

    async def test_full_flow_stores_record_and_sends_tessera_pkce(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stateless exchange gets tessera's own code and PKCE verifier."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        expiry = datetime(2026, 1, 1, tzinfo=timezone.utc)
        stub = _RecordingProvider(_token(expires_at=expiry, scope=["openid", "profile"]))
        async with TokenStore(config.store, clock=_Clock(100.0)) as tokens:
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: stub)
            url = await orch.begin_login("alice", "manual-idp")
            state = _url_param(url, "state")
            record = await orch.complete_callback("alice", state, "auth-code-1")
            loaded = await tokens.load("alice", "manual-idp")
        assert record.refresh_token == "rt-1"
        assert record.expires_at == expiry.timestamp()
        assert record.scope == "openid profile"
        assert loaded == record
        code, verifier = stub.exchanges[0]
        assert code == "auth-code-1"
        assert verifier is not None
        assert compute_code_challenge(verifier) == _url_param(url, "code_challenge")
        assert stub.closed is True

    async def test_token_without_expiry_or_scope_maps_to_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing expiry and scope are stored as None, not fabricated."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        stub = _RecordingProvider(_token())
        async with TokenStore(config.store) as tokens:
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: stub)
            url = await orch.begin_login("alice", "manual-idp")
            record = await orch.complete_callback("alice", _url_param(url, "state"), "c-1")
        assert record.expires_at is None
        assert record.scope is None
        assert record.refresh_expires_at is None

    async def test_positive_refresh_expiry_metadata_is_stored_as_a_date(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A positive refresh_expires_in becomes now + value (clock mocked)."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        stub = _RecordingProvider(_token(metadata={"refresh_expires_in": 300}))
        clock = _Clock(100.0)
        async with TokenStore(config.store, clock=clock) as tokens:
            orch, _, _ = _orchestrator(
                config, tokens, factory=lambda server, secret: stub, clock=clock
            )
            url = await orch.begin_login("alice", "manual-idp")
            record = await orch.complete_callback("alice", _url_param(url, "state"), "c-1")
        assert record.refresh_expires_at == 400.0

    @pytest.mark.parametrize(
        "metadata",
        [
            {},  # provider says nothing about the refresh expiry
            {"refresh_expires_in": 0},  # Keycloak offline token: no expiry
            {"refresh_expires_in": -5},  # nonsense value, not extrapolated
            {"refresh_expires_in": "soon"},  # ill-typed value, not extrapolated
            {"refresh_expires_in": True},  # a bool is not a duration
        ],
    )
    async def test_absent_or_unusable_refresh_expiry_maps_to_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, metadata: dict[str, object]
    ) -> None:
        """Zero, absent, or unusable refresh_expires_in stores None."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        stub = _RecordingProvider(_token(metadata=metadata))
        async with TokenStore(config.store) as tokens:
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: stub)
            url = await orch.begin_login("alice", "manual-idp")
            record = await orch.complete_callback("alice", _url_param(url, "state"), "c-1")
        assert record.refresh_expires_at is None


class TestDefaultProviderPath:
    """Without a factory the orchestrator builds real kstlib providers."""

    async def test_explicit_server_builds_oauth2_provider_with_fresh_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each flow builds a fresh kstlib config; the secret is read once."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        records: list[tuple[str, AuthProviderConfig, object]] = []
        monkeypatch.setattr(orchestrator_module, "OAuth2Provider", _fake_provider_cls(records))
        async with TokenStore(config.store) as tokens:
            orch, _, _ = _orchestrator(config, tokens)
            url = await orch.begin_login("alice", "manual-idp")
            await orch.complete_callback("alice", _url_param(url, "state"), "c-1")
            monkeypatch.delenv(_ENV_NAME)  # cached secret: never re-read from the env
            url2 = await orch.begin_login("alice", "manual-idp")
            await orch.complete_callback("alice", _url_param(url2, "state"), "c-2")
        assert [entry[0] for entry in records] == ["manual-idp", "manual-idp"]
        name, cfg, storage = records[0]
        assert cfg.authorize_url == _AUTHORIZE_URL
        assert cfg.token_url == _TOKEN_URL
        assert cfg.client_secret == _SECRET
        assert cfg.redirect_uri == _REDIRECT_URI
        assert cfg.scopes == ["openid", "profile"]
        assert cfg.pkce is False
        assert isinstance(storage, MemoryTokenStorage)
        # One fresh config per operation: kstlib's OIDC constructor mutates
        # the config it receives, so a shared object would carry one
        # construction's state into the next flow.
        assert records[1][1] is not cfg
        assert records[1][1].client_secret == _SECRET

    async def test_discovery_server_builds_oidc_provider(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A discovery-mode server goes through OIDCProvider, not OAuth2Provider."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        oidc_records: list[tuple[str, AuthProviderConfig, object]] = []
        monkeypatch.setattr(orchestrator_module, "OIDCProvider", _fake_provider_cls(oidc_records))
        async with TokenStore(config.store) as tokens:
            orch, pending, clock = _orchestrator(config, tokens)
            pending.add(
                "state-1",
                PendingFlow(
                    username="alice",
                    provider="discovery-idp",
                    code_verifier=generate_code_verifier(),
                    created_at=clock.now,
                ),
            )
            record = await orch.complete_callback("alice", "state-1", "c-1")
        assert [entry[0] for entry in oidc_records] == ["discovery-idp"]
        assert oidc_records[0][1].issuer == _ISSUER
        assert record.refresh_token == "rt-1"


def _fake_provider_cls(records: list[tuple[str, AuthProviderConfig, object]]) -> type:
    """A stand-in for a kstlib provider class, recording constructions."""

    class _FakeProvider:
        def __init__(self, name: str, cfg: AuthProviderConfig, storage: object) -> None:
            records.append((name, cfg, storage))

        def exchange_code_stateless(self, code: str, *, code_verifier: str | None = None) -> Token:
            return _token()

        def close(self) -> None:
            return None

    return _FakeProvider


class TestKstlibRegressions:
    """Real kstlib providers over MockTransport: the pitfalls stay fixed."""

    async def test_legitimate_flow_is_security_silent_and_leaks_nothing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A full explicit-mode flow emits no [SECURITY] log and no secret value."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        requests_seen: list[httpx.Request] = []
        transport = _discovery_handler(requests_seen)
        # Built once, before the capture window: constructing this config logs
        # kstlib's informative not-localhost [SECURITY] warning exactly once
        # per process; steady-state flows must then stay silent.
        provider_config = AuthProviderConfig(
            client_id="tessera-demo-client",
            client_secret=_SECRET,
            authorize_url=_AUTHORIZE_URL,
            token_url=_TOKEN_URL,
            scopes=["openid", "profile"],
            redirect_uri=_REDIRECT_URI,
            pkce=False,
        )

        def factory(server: ServerConfig, secret: str) -> OAuth2Provider:
            return OAuth2Provider(
                server.name,
                provider_config,
                MemoryTokenStorage(),
                http_client=httpx.Client(transport=transport),
            )

        async with TokenStore(config.store) as tokens:
            orch, _, _ = _orchestrator(config, tokens, factory=factory)
            # caplog captures the whole test: drop the one-shot construction
            # warning above so the assertions measure the steady-state flow.
            caplog.clear()
            with caplog.at_level(logging.DEBUG):
                url = await orch.begin_login("alice", "manual-idp")
                state = _url_param(url, "state")
                record = await orch.complete_callback("alice", state, "auth-code-xyz")
        assert record.refresh_token == "rt-1"
        body = dict(parse_qsl(requests_seen[-1].read().decode("ascii")))
        verifier = body["code_verifier"]
        assert compute_code_challenge(verifier) == _url_param(url, "code_challenge")
        assert body["code"] == "auth-code-xyz"
        assert body["client_secret"] == _SECRET
        assert body["redirect_uri"] == _REDIRECT_URI
        assert "state" not in body  # kstlib validates state locally, off the wire
        assert "[SECURITY]" not in caplog.text
        for value in (state, "auth-code-xyz", verifier, _SECRET, "at-1", "rt-1"):
            assert value not in caplog.text

    async def test_discovery_exchange_hits_the_discovered_token_endpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stateless exchange resolves the real token endpoint itself.

        No begin_login happens in this process (a service restart between
        login and callback), so the kstlib config still holds the
        issuer-derived placeholder token URL; the stateless exchange must
        run discovery before it posts anywhere.
        """
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        requests_seen: list[httpx.Request] = []
        transport = _discovery_handler(requests_seen)
        oidc_config = AuthProviderConfig(
            client_id="tessera-demo-client",
            client_secret=_SECRET,
            issuer=_ISSUER,
            scopes=["openid"],
            redirect_uri=_REDIRECT_URI,
            pkce=False,
        )

        def factory(server: ServerConfig, secret: str) -> OAuth2Provider:
            return OIDCProvider(
                server.name,
                oidc_config,
                MemoryTokenStorage(),
                http_client=httpx.Client(transport=transport),
            )

        state = generate_state()
        async with TokenStore(config.store) as tokens:
            orch, pending, clock = _orchestrator(config, tokens, factory=factory)
            pending.add(
                state,
                PendingFlow(
                    username="alice",
                    provider="discovery-idp",
                    code_verifier=generate_code_verifier(),
                    created_at=clock.now,
                ),
            )
            record = await orch.complete_callback("alice", state, "auth-code-1")
        token_posts = [r for r in requests_seen if r.method == "POST"]
        assert [str(r.url) for r in token_posts] == [_DISCOVERED_TOKEN]
        assert record.refresh_token == "rt-1"

    async def test_transient_discovery_failure_does_not_poison_later_logins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A login right after a transient discovery outage recovers by itself.

        Runs the real construction path (no injected factory): the first
        well-known fetch fails like an unreachable IdP, and the next login
        must reach the endpoint of the served discovery document, never an
        issuer-derived placeholder frozen by the failed attempt.
        """
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        well_known_hits: list[int] = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/.well-known/openid-configuration"):
                well_known_hits[0] += 1
                if well_known_hits[0] == 1:
                    return httpx.Response(503)  # the IdP is transiently down
                return httpx.Response(
                    200,
                    json={
                        "issuer": _ISSUER,
                        "authorization_endpoint": _DISCOVERED_AUTHORIZE,
                        "token_endpoint": _DISCOVERED_TOKEN,
                        "jwks_uri": f"{_ISSUER}/protocol/openid-connect/certs",
                    },
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)

        class _PinnedTransportOIDCProvider(OIDCProvider):
            """The real kstlib provider, pinned to the mock transport."""

            def __init__(
                self,
                name: str,
                provider_config: AuthProviderConfig,
                token_storage: MemoryTokenStorage,
            ) -> None:
                super().__init__(
                    name,
                    provider_config,
                    token_storage,
                    http_client=httpx.Client(transport=transport),
                )

        monkeypatch.setattr(orchestrator_module, "OIDCProvider", _PinnedTransportOIDCProvider)
        async with TokenStore(config.store) as tokens:
            orch, _, _ = _orchestrator(config, tokens)
            with pytest.raises(ExchangeError):
                await orch.begin_login("alice", "discovery-idp")
            url = await orch.begin_login("alice", "discovery-idp")
        assert url.startswith(f"{_DISCOVERED_AUTHORIZE}?")


class _RefreshingProvider(OAuth2Provider):
    """A no-network provider recording the refresh calls it receives."""

    def __init__(self, result: Token | Exception) -> None:
        super().__init__(
            "stub",
            AuthProviderConfig(
                client_id="tessera-demo-client",
                authorize_url=_AUTHORIZE_URL,
                token_url=_TOKEN_URL,
                pkce=False,
            ),
            MemoryTokenStorage(),
        )
        self.result = result
        self.refreshed: list[Token | None] = []
        self.close_calls = 0

    def refresh(self, token: Token | None = None) -> Token:
        """Record the passed token and return (or raise) the result."""
        self.refreshed.append(token)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self) -> None:
        """Count and delegate."""
        self.close_calls += 1
        super().close()


class _GatedTokenStore(TokenStore):
    """A TokenStore whose load() can be held on per-pair gates.

    Lets a concurrency test freeze one pair's refresh at an await point
    while another pair runs to completion, deterministically and without
    any real sleep.
    """

    def __init__(self, config: StoreConfig, gates: dict[tuple[str, str], asyncio.Event]) -> None:
        super().__init__(config)
        self.gates = gates

    async def load(self, username: str, provider: str) -> TokenRecord | None:
        """Wait on the pair's gate, if any, then delegate."""
        gate = self.gates.get((username, provider))
        if gate is not None:
            await gate.wait()
        return await super().load(username, provider)


class _CountingDeleteStore(TokenStore):
    """A TokenStore counting delete() calls through the public surface."""

    def __init__(self, config: StoreConfig) -> None:
        super().__init__(config)
        self.delete_calls = 0

    async def delete(self, username: str, provider: str) -> bool:
        """Count and delegate."""
        self.delete_calls += 1
        return await super().delete(username, provider)


def _fresh_token(
    *,
    refresh: str | None = "rt-same",
    expires_at: datetime | None = None,
    scope: list[str] | None = None,
    metadata: dict[str, object] | None = None,
) -> Token:
    """Build a kstlib token as returned by a refresh."""
    return Token(
        access_token="at-fresh",
        expires_at=expires_at,
        refresh_token=refresh,
        scope=scope or [],
        metadata=dict(metadata or {}),
    )


class TestGetAccessTokenRejections:
    """get_access_token refuses cleanly, before and after the provider call."""

    async def test_unknown_server_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An undeclared server raises UnknownServerError; no provider is built."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        orch, _, _ = _orchestrator(config, TokenStore(config.store), factory=_forbidden_factory)
        with pytest.raises(UnknownServerError):
            await orch.get_access_token("alice", "nowhere")

    async def test_no_stored_token_is_actionable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pair without a stored token tells the user to sign in via the button."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        async with TokenStore(config.store) as tokens:
            orch, _, _ = _orchestrator(config, tokens, factory=_forbidden_factory)
            with pytest.raises(NoStoredTokenError, match="sign in") as excinfo:
                await orch.get_access_token("alice", "manual-idp")
        assert "manual-idp" in str(excinfo.value)

    async def test_rejected_refresh_invalidates_stored_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A definitive rejection deletes the record and turns the status red."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        provider = _RefreshingProvider(TokenRefreshError("invalid_grant", retryable=False))
        async with TokenStore(config.store) as tokens:
            await tokens.save("alice", "manual-idp", "rt-stored-secret")
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: provider)
            with caplog.at_level(logging.DEBUG), pytest.raises(RefreshRejectedError):
                await orch.get_access_token("alice", "manual-idp")
            assert await tokens.load("alice", "manual-idp") is None
            status = await orch.get_status("alice", "manual-idp")
        assert status.has_token is False
        assert status.expires_at is None
        assert "[SECURITY]" in caplog.text
        assert "rt-stored-secret" not in caplog.text
        assert provider.close_calls == 1

    async def test_transient_failure_keeps_stored_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A retryable failure raises ExchangeError and keeps the record intact."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        provider = _RefreshingProvider(TokenRefreshError("gateway timeout", retryable=True))
        async with TokenStore(config.store) as tokens:
            await tokens.save("alice", "manual-idp", "rt-stored-secret")
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: provider)
            with caplog.at_level(logging.DEBUG), pytest.raises(ExchangeError):
                await orch.get_access_token("alice", "manual-idp")
            loaded = await tokens.load("alice", "manual-idp")
        assert loaded is not None
        assert loaded.refresh_token == "rt-stored-secret"
        assert "[SECURITY]" not in caplog.text
        assert "rt-stored-secret" not in caplog.text
        assert provider.close_calls == 1

    async def test_other_auth_error_keeps_stored_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-refresh kstlib failure wraps into ExchangeError; the record survives."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        provider = _RefreshingProvider(TokenExchangeError("discovery exploded"))
        async with TokenStore(config.store) as tokens:
            await tokens.save("alice", "manual-idp", "rt-stored-secret")
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: provider)
            with pytest.raises(ExchangeError):
                await orch.get_access_token("alice", "manual-idp")
            loaded = await tokens.load("alice", "manual-idp")
        assert loaded is not None
        assert loaded.refresh_token == "rt-stored-secret"
        assert provider.close_calls == 1


class TestGetAccessTokenHappyPath:
    """A stored refresh token yields a fresh access token, rotating when told to."""

    async def test_refresh_without_rotation_rewrites_the_record_freshly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same refresh token back: the record is rewritten with fresh metadata.

        The refresh window of a session-bound token slides on every
        successful refresh, so the record is upserted each time: the token
        value stays, ``refresh_expires_at`` and ``updated_at`` move, and
        ``created_at`` is preserved by the upsert.
        """
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        expiry = datetime(2026, 1, 1, tzinfo=timezone.utc)
        provider = _RefreshingProvider(
            _fresh_token(
                refresh="rt-same",
                expires_at=expiry,
                scope=["openid"],
                metadata={"refresh_expires_in": 300},
            )
        )
        clock = _Clock(100.0)
        async with TokenStore(config.store, clock=clock) as tokens:
            await tokens.save(
                "alice",
                "manual-idp",
                "rt-same",
                expires_at=50.0,
                scope="openid",
                refresh_expires_at=150.0,
            )
            orch, _, _ = _orchestrator(
                config, tokens, factory=lambda server, secret: provider, clock=clock
            )
            clock.now = 200.0
            result = await orch.get_access_token("alice", "manual-idp")
            after = await tokens.load("alice", "manual-idp")
        assert result.value == "at-fresh"
        assert result.expires_at == expiry.timestamp()
        assert result.scope == "openid"
        assert after is not None
        assert after.refresh_token == "rt-same"  # unchanged token value
        assert after.expires_at == expiry.timestamp()  # fresh indicative expiry
        assert after.refresh_expires_at == 500.0  # slid window: 200 + 300
        assert after.created_at == 100.0  # preserved by the upsert
        assert after.updated_at == 200.0  # moved by the refresh write

    async def test_refresh_with_none_refresh_token_keeps_stored_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A response without any refresh token keeps the stored token value.

        The record is still rewritten (the refresh succeeded, so its
        window slid), but the refresh token column keeps the stored value.
        """
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        provider = _RefreshingProvider(_fresh_token(refresh=None))
        clock = _Clock(100.0)
        async with TokenStore(config.store, clock=clock) as tokens:
            await tokens.save("alice", "manual-idp", "rt-stored", refresh_expires_at=150.0)
            orch, _, _ = _orchestrator(
                config, tokens, factory=lambda server, secret: provider, clock=clock
            )
            clock.now = 200.0
            result = await orch.get_access_token("alice", "manual-idp")
            after = await tokens.load("alice", "manual-idp")
        assert result.value == "at-fresh"
        assert after is not None
        assert after.refresh_token == "rt-stored"
        assert after.refresh_expires_at is None  # the fresh response gave none
        assert after.created_at == 100.0
        assert after.updated_at == 200.0

    async def test_rotation_replaces_refresh_token_atomically(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A rotated refresh token replaces the old one; created_at is preserved."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        expiry = datetime(2026, 1, 1, tzinfo=timezone.utc)
        provider = _RefreshingProvider(
            _fresh_token(
                refresh="rt-new",
                expires_at=expiry,
                scope=["openid", "profile"],
                metadata={"refresh_expires_in": 60},
            )
        )
        clock = _Clock(100.0)
        async with TokenStore(config.store, clock=clock) as tokens:
            await tokens.save("alice", "manual-idp", "rt-old")
            orch, _, _ = _orchestrator(
                config, tokens, factory=lambda server, secret: provider, clock=clock
            )
            clock.now = 200.0
            with caplog.at_level(logging.DEBUG):
                result = await orch.get_access_token("alice", "manual-idp")
            loaded = await tokens.load("alice", "manual-idp")
        assert result.value == "at-fresh"
        assert loaded is not None
        assert loaded.refresh_token == "rt-new"
        assert loaded.created_at == 100.0  # preserved by the upsert
        assert loaded.updated_at == 200.0  # moved by the rotation write
        assert loaded.expires_at == expiry.timestamp()
        assert loaded.scope == "openid profile"
        assert loaded.refresh_expires_at == 260.0  # 200 + 60 from the metadata
        for value in ("rt-old", "rt-new", "at-fresh"):
            assert value not in caplog.text

    async def test_mapping_carries_stored_refresh_token_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The provider gets the stored refresh token and an empty access-token placeholder."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        provider = _RefreshingProvider(_fresh_token(refresh="rt-stored"))
        async with TokenStore(config.store) as tokens:
            await tokens.save("alice", "manual-idp", "rt-stored")
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: provider)
            await orch.get_access_token("alice", "manual-idp")
        passed = provider.refreshed[0]
        assert passed is not None
        assert passed.refresh_token == "rt-stored"
        assert passed.access_token == ""  # tessera never stores an access token
        assert provider.close_calls == 1


class TestGetStatus:
    """get_status is a cheap store read: no provider call, honest semantics."""

    async def test_status_reports_the_full_stored_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stored record reports every detail field the panel consumes."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        clock = _Clock(100.0)
        async with TokenStore(config.store, clock=clock) as tokens:
            await tokens.save(
                "alice",
                "manual-idp",
                "rt-stored",
                expires_at=500.0,
                scope="openid profile offline_access",
                refresh_expires_at=900.0,
            )
            orch, _, _ = _orchestrator(config, tokens, factory=_forbidden_factory)
            status = await orch.get_status("alice", "manual-idp")
        assert status.has_token is True
        assert status.expires_at == 500.0
        assert status.created_at == 100.0
        assert status.updated_at == 100.0
        assert status.scope == "openid profile offline_access"
        assert status.refresh_kind == "offline"
        assert status.refresh_expires_at == 900.0

    async def test_status_reports_missing_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pair without a stored token reports red with every detail None."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        async with TokenStore(config.store) as tokens:
            orch, _, _ = _orchestrator(config, tokens, factory=_forbidden_factory)
            status = await orch.get_status("alice", "manual-idp")
        assert status.has_token is False
        assert status.expires_at is None
        assert status.created_at is None
        assert status.updated_at is None
        assert status.scope is None
        assert status.refresh_kind == "unknown"
        assert status.refresh_expires_at is None

    @pytest.mark.parametrize(
        ("scope", "kind"),
        [
            ("openid profile offline_access", "offline"),
            ("offline_access", "offline"),
            ("openid profile", "session"),
            ("openid profile offline_access_extended", "session"),  # no substring match
            (None, "unknown"),
        ],
    )
    async def test_refresh_kind_derives_from_the_stored_scope(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        scope: str | None,
        kind: str,
    ) -> None:
        """The refresh kind is offline, session, or unknown, by scope membership."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        async with TokenStore(config.store) as tokens:
            await tokens.save("alice", "manual-idp", "rt-stored", scope=scope)
            orch, _, _ = _orchestrator(config, tokens, factory=_forbidden_factory)
            status = await orch.get_status("alice", "manual-idp")
        assert status.has_token is True
        assert status.refresh_kind == kind

    async def test_status_unknown_server_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An undeclared server raises UnknownServerError instead of a silent red."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        orch, _, _ = _orchestrator(config, TokenStore(config.store), factory=_forbidden_factory)
        with pytest.raises(UnknownServerError):
            await orch.get_status("alice", "nowhere")


class TestVerify:
    """verify runs a real refresh and answers a plain boolean, never a token."""

    async def test_verify_true_when_refresh_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stored token the provider still accepts verifies as True."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        provider = _RefreshingProvider(_fresh_token(refresh="rt-stored"))
        async with TokenStore(config.store) as tokens:
            await tokens.save("alice", "manual-idp", "rt-stored")
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: provider)
            assert await orch.verify("alice", "manual-idp") is True
            assert await tokens.exists("alice", "manual-idp") is True

    async def test_verify_false_on_definitive_rejection_and_record_purged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A definitive rejection is an answer: False, and the record is gone."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        provider = _RefreshingProvider(TokenRefreshError("invalid_grant", retryable=False))
        async with TokenStore(config.store) as tokens:
            await tokens.save("alice", "manual-idp", "rt-stored")
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: provider)
            assert await orch.verify("alice", "manual-idp") is False
            assert await tokens.exists("alice", "manual-idp") is False

    async def test_verify_transient_failure_propagates_and_keeps_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A doubt is never answered False: the transient error propagates."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        provider = _RefreshingProvider(TokenRefreshError("gateway timeout", retryable=True))
        async with TokenStore(config.store) as tokens:
            await tokens.save("alice", "manual-idp", "rt-stored")
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: provider)
            with pytest.raises(ExchangeError):
                await orch.verify("alice", "manual-idp")
            assert await tokens.exists("alice", "manual-idp") is True

    async def test_verify_without_stored_token_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing to verify: NoStoredTokenError reaches the caller."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        async with TokenStore(config.store) as tokens:
            orch, _, _ = _orchestrator(config, tokens, factory=_forbidden_factory)
            with pytest.raises(NoStoredTokenError):
                await orch.verify("alice", "manual-idp")

    async def test_verify_unknown_server_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An undeclared server raises UnknownServerError."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        orch, _, _ = _orchestrator(config, TokenStore(config.store), factory=_forbidden_factory)
        with pytest.raises(UnknownServerError):
            await orch.verify("alice", "nowhere")

    async def test_verify_shares_the_single_flight(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A double-click coalesces into one provider refresh."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        provider = _RefreshingProvider(_fresh_token(refresh="rt-stored"))
        async with TokenStore(config.store) as tokens:
            await tokens.save("alice", "manual-idp", "rt-stored")
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: provider)
            first, second = await asyncio.gather(
                orch.verify("alice", "manual-idp"),
                orch.verify("alice", "manual-idp"),
            )
        assert first is True and second is True
        assert len(provider.refreshed) == 1


def _revocable_provider(
    requests_seen: list[httpx.Request],
    *,
    revoke_url: str | None = f"{_ISSUER}/protocol/openid-connect/revoke",
    fail_transport: bool = False,
) -> OAuth2Provider:
    """Build a real explicit-mode provider whose revocation is observable.

    Args:
        requests_seen: Recorder receiving every HTTP request the provider makes.
        revoke_url: The revocation endpoint, or None to leave it unconfigured.
        fail_transport: When True, every request raises a connection error.

    Returns:
        The provider, wired over a mock transport (zero network).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if fail_transport:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200)

    return OAuth2Provider(
        "stub",
        AuthProviderConfig(
            client_id="tessera-demo-client",
            client_secret=_SECRET,
            authorize_url=_AUTHORIZE_URL,
            token_url=_TOKEN_URL,
            revoke_url=revoke_url,
            pkce=False,
        ),
        MemoryTokenStorage(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


class TestRevoke:
    """revoke notifies the provider best-effort, then always purges locally."""

    async def test_revoke_posts_the_refresh_token_once_and_notifies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exactly one RFC 7009 post targets the refresh token.

        tessera only holds a refresh token, so the revocation targets that
        single kind: one post, carrying the real value with the honest
        ``refresh_token`` hint, and the reported success speaks for that
        kind alone (no second call, no empty placeholder earning its own
        200).
        """
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        requests_seen: list[httpx.Request] = []
        provider = _revocable_provider(requests_seen)
        async with TokenStore(config.store) as tokens:
            await tokens.save("alice", "manual-idp", "rt-stored-secret")
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: provider)
            notified = await orch.revoke("alice", "manual-idp")
            assert notified is True
            assert await tokens.exists("alice", "manual-idp") is False
        bodies = [dict(parse_qsl(request.read().decode("ascii"))) for request in requests_seen]
        assert [body["token"] for body in bodies] == ["rt-stored-secret"]
        assert [body["token_type_hint"] for body in bodies] == ["refresh_token"]
        assert all(body["client_id"] == "tessera-demo-client" for body in bodies)
        assert all(body["client_secret"] == _SECRET for body in bodies)

    async def test_revoke_without_revocation_endpoint_still_purges(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No revocation endpoint: not notified, but the record is purged."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        requests_seen: list[httpx.Request] = []
        provider = _revocable_provider(requests_seen, revoke_url=None)
        async with TokenStore(config.store) as tokens:
            await tokens.save("alice", "manual-idp", "rt-stored")
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: provider)
            notified = await orch.revoke("alice", "manual-idp")
            assert notified is False
            assert await tokens.exists("alice", "manual-idp") is False
        assert requests_seen == []  # no endpoint: no network call at all

    async def test_revoke_discovers_the_endpoint_in_discovery_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Discovery mode resolves revocation_endpoint before revoking.

        The base revoke reads config.revoke_url directly, and only
        discover() populates it in discovery mode, so a helper that
        skipped discovery would silently report not-notified here.
        """
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        requests_seen: list[httpx.Request] = []
        revoke_endpoint = f"{_ISSUER}/protocol/openid-connect/revoke"

        def handler(request: httpx.Request) -> httpx.Response:
            requests_seen.append(request)
            if request.url.path.endswith("/.well-known/openid-configuration"):
                return httpx.Response(
                    200,
                    json={
                        "issuer": _ISSUER,
                        "authorization_endpoint": _DISCOVERED_AUTHORIZE,
                        "token_endpoint": _DISCOVERED_TOKEN,
                        "revocation_endpoint": revoke_endpoint,
                        "jwks_uri": f"{_ISSUER}/protocol/openid-connect/certs",
                    },
                )
            return httpx.Response(200)

        def factory(server: ServerConfig, secret: str) -> OAuth2Provider:
            return OIDCProvider(
                server.name,
                AuthProviderConfig(
                    client_id=server.client_id,
                    client_secret=secret,
                    issuer=_ISSUER,
                    pkce=False,
                ),
                MemoryTokenStorage(),
                http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            )

        async with TokenStore(config.store) as tokens:
            await tokens.save("alice", "discovery-idp", "rt-stored")
            orch, _, _ = _orchestrator(config, tokens, factory=factory)
            notified = await orch.revoke("alice", "discovery-idp")
            assert notified is True
            assert await tokens.exists("alice", "discovery-idp") is False
        revoke_posts = [r for r in requests_seen if r.method == "POST"]
        assert [str(r.url) for r in revoke_posts] == [revoke_endpoint]

    async def test_revoke_transport_failure_still_purges(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreachable revocation endpoint: not notified, still purged."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        requests_seen: list[httpx.Request] = []
        provider = _revocable_provider(requests_seen, fail_transport=True)
        async with TokenStore(config.store) as tokens:
            await tokens.save("alice", "manual-idp", "rt-stored")
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: provider)
            notified = await orch.revoke("alice", "manual-idp")
            assert notified is False
            assert await tokens.exists("alice", "manual-idp") is False

    async def test_revoke_discovery_failure_still_purges(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed discovery is best-effort: not notified, still purged."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)

        def factory(server: ServerConfig, secret: str) -> OAuth2Provider:
            return OIDCProvider(
                server.name,
                AuthProviderConfig(client_id=server.client_id, issuer=_ISSUER, pkce=False),
                MemoryTokenStorage(),
                http_client=httpx.Client(
                    transport=httpx.MockTransport(lambda request: httpx.Response(500))
                ),
            )

        async with TokenStore(config.store) as tokens:
            await tokens.save("alice", "discovery-idp", "rt-stored")
            orch, _, _ = _orchestrator(config, tokens, factory=factory)
            notified = await orch.revoke("alice", "discovery-idp")
            assert notified is False
            assert await tokens.exists("alice", "discovery-idp") is False

    async def test_revoke_unresolvable_secret_still_purges(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A lost client secret never blocks the local purge (best-effort)."""
        monkeypatch.delenv(_ENV_NAME, raising=False)
        config = _tessera_config(tmp_path)
        async with TokenStore(config.store) as tokens:
            await tokens.save("alice", "manual-idp", "rt-stored")
            orch, _, _ = _orchestrator(config, tokens, factory=_forbidden_factory)
            notified = await orch.revoke("alice", "manual-idp")
            assert notified is False
            assert await tokens.exists("alice", "manual-idp") is False

    async def test_revoke_without_stored_token_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing to revoke: NoStoredTokenError, and no provider is built."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        async with TokenStore(config.store) as tokens:
            orch, _, _ = _orchestrator(config, tokens, factory=_forbidden_factory)
            with pytest.raises(NoStoredTokenError):
                await orch.revoke("alice", "manual-idp")

    async def test_second_revoke_raises_no_stored_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Revoking twice: the second call finds nothing and raises."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        requests_seen: list[httpx.Request] = []
        async with TokenStore(config.store) as tokens:
            await tokens.save("alice", "manual-idp", "rt-stored")
            orch, _, _ = _orchestrator(
                config,
                tokens,
                factory=lambda server, secret: _revocable_provider(requests_seen),
            )
            await orch.revoke("alice", "manual-idp")
            with pytest.raises(NoStoredTokenError):
                await orch.revoke("alice", "manual-idp")

    async def test_revoke_unknown_server_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An undeclared server raises UnknownServerError."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        orch, _, _ = _orchestrator(config, TokenStore(config.store), factory=_forbidden_factory)
        with pytest.raises(UnknownServerError):
            await orch.revoke("alice", "nowhere")

    async def test_revoke_logs_report_the_action_without_token_values(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The deliberate action is logged at INFO, never with a token value."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        requests_seen: list[httpx.Request] = []
        provider = _revocable_provider(requests_seen)
        async with TokenStore(config.store) as tokens:
            await tokens.save("alice", "manual-idp", "rt-stored-secret")
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: provider)
            with caplog.at_level(logging.DEBUG):
                await orch.revoke("alice", "manual-idp")
        assert "revoked stored token" in caplog.text
        assert "rt-stored-secret" not in caplog.text


class TestListServers:
    """list_servers exposes the declared buttons, in declaration order."""

    async def test_declared_servers_in_declaration_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every declared server is listed once, in the file's order."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        orch, _, _ = _orchestrator(config, TokenStore(config.store), factory=_forbidden_factory)
        servers = orch.list_servers()
        assert [info.name for info in servers] == ["manual-idp", "discovery-idp"]

    async def test_button_fields_come_from_the_configuration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Label and colors mirror the validated ButtonConfig defaults."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        orch, _, _ = _orchestrator(config, TokenStore(config.store), factory=_forbidden_factory)
        info = orch.list_servers()[0]
        button = config.get("manual-idp").button
        assert info.label == button.label
        assert info.color_valid == button.color_valid
        assert info.color_invalid == button.color_invalid


class TestSingleFlight:
    """Concurrent refreshes for one pair share a single provider round-trip."""

    async def test_concurrent_same_pair_refresh_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two concurrent calls for one pair trigger exactly one refresh.

        Deterministic: the second caller finds the in-flight task in the
        map synchronously, before the event loop first runs that task.
        """
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        provider = _RefreshingProvider(_fresh_token(refresh="rt-stored"))
        async with TokenStore(config.store) as tokens:
            await tokens.save("alice", "manual-idp", "rt-stored")
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: provider)
            first, second = await asyncio.gather(
                orch.get_access_token("alice", "manual-idp"),
                orch.get_access_token("alice", "manual-idp"),
            )
        assert len(provider.refreshed) == 1
        assert first == second  # the same shared result, not two round-trips

    async def test_different_pairs_do_not_serialize(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A blocked pair never delays another pair (no shared lock)."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        providers: list[_RefreshingProvider] = []

        def factory(server: ServerConfig, secret: str) -> OAuth2Provider:
            provider = _RefreshingProvider(_fresh_token(refresh="rt-stored"))
            providers.append(provider)
            return provider

        gate = asyncio.Event()
        store = _GatedTokenStore(config.store, {("alice", "manual-idp"): gate})
        async with store as tokens:
            await tokens.save("alice", "manual-idp", "rt-stored")
            await tokens.save("bob", "manual-idp", "rt-stored")
            orch, _, _ = _orchestrator(config, tokens, factory=factory)
            blocked = asyncio.create_task(orch.get_access_token("alice", "manual-idp"))
            free = asyncio.create_task(orch.get_access_token("bob", "manual-idp"))
            # bob completes while alice's store read is still gated. The cap
            # is a pure regression guard (a cross-pair lock would hang here);
            # it never waits when the code is correct.
            result_bob = await asyncio.wait_for(free, timeout=5.0)
            assert blocked.done() is False
            gate.set()
            result_alice = await blocked
        assert result_bob.value == "at-fresh"
        assert result_alice.value == "at-fresh"
        assert len(providers) == 2

    async def test_concurrent_failure_is_shared_and_deletes_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both concurrent callers get the same rejection; the record is deleted once."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        provider = _RefreshingProvider(TokenRefreshError("invalid_grant", retryable=False))
        store = _CountingDeleteStore(config.store)
        async with store as tokens:
            await tokens.save("alice", "manual-idp", "rt-stored")
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: provider)
            results = await asyncio.gather(
                orch.get_access_token("alice", "manual-idp"),
                orch.get_access_token("alice", "manual-idp"),
                return_exceptions=True,
            )
        assert len(provider.refreshed) == 1
        assert store.delete_calls == 1
        assert all(isinstance(r, RefreshRejectedError) for r in results)
        assert results[0] is results[1]  # one shared outcome, not two failures

    async def test_completed_flight_is_forgotten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After completion a new call refreshes again: the in-flight entry is gone.

        This is the public-API proof that the coalescing map stays bounded:
        entries live exactly as long as their refresh operation.
        """
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        provider = _RefreshingProvider(_fresh_token(refresh="rt-stored"))
        async with TokenStore(config.store) as tokens:
            await tokens.save("alice", "manual-idp", "rt-stored")
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: provider)
            await orch.get_access_token("alice", "manual-idp")
            await orch.get_access_token("alice", "manual-idp")
        assert len(provider.refreshed) == 2

    async def test_cancelled_refresh_frees_the_slot_and_stays_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refresh cancelled mid-flight frees the slot; the next call starts anew.

        Deterministic by construction: the in-flight task is held on the
        gated store read (an asyncio.Event), the task handle is taken from
        ``asyncio.all_tasks()`` (no tessera internals), and the cancellation
        is awaited through the public call. No real sleep, no wall clock.
        """
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        provider = _RefreshingProvider(_fresh_token(refresh="rt-stored"))
        gate = asyncio.Event()
        store = _GatedTokenStore(config.store, {("alice", "manual-idp"): gate})
        async with store as tokens:
            await tokens.save("alice", "manual-idp", "rt-stored")
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: provider)
            before = asyncio.all_tasks()
            caller = asyncio.create_task(orch.get_access_token("alice", "manual-idp"))
            inner_tasks: set[asyncio.Task[object]] = set()
            for _ in range(50):
                await asyncio.sleep(0)
                inner_tasks = asyncio.all_tasks() - before - {caller}
                if inner_tasks:
                    break
            (inner,) = inner_tasks
            inner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            # The done callback freed the single-flight slot: with the gate
            # open, a new call runs a fresh refresh and succeeds.
            gate.set()
            token = await orch.get_access_token("alice", "manual-idp")
        assert token.value == "at-fresh"
        # The cancelled flight never reached the provider; only the second did.
        assert len(provider.refreshed) == 1


_T = TypeVar("_T")


class _CountingExecutor(ThreadPoolExecutor):
    """A two-worker thread pool counting the tasks submitted through it."""

    def __init__(self) -> None:
        super().__init__(max_workers=2)
        self.submitted = 0

    def submit(self, fn: Callable[..., _T], /, *args: object, **kwargs: object) -> Future[_T]:
        """Count and delegate."""
        self.submitted += 1
        return super().submit(fn, *args, **kwargs)


class TestExecutorBoundary:
    """Blocking kstlib work runs in the injected executor; state stays on the loop."""

    async def test_explicit_login_needs_no_executor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit-mode login is served entirely on the event loop."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        with _CountingExecutor() as executor:
            orch, _, _ = _orchestrator(
                config, TokenStore(config.store), factory=_forbidden_factory, executor=executor
            )
            url = await orch.begin_login("alice", "manual-idp")
            assert url.startswith(f"{_AUTHORIZE_URL}?")
            assert executor.submitted == 0

    async def test_discovery_login_runs_in_executor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A discovery-mode login submits the secret read and the discovery."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        stub = _RecordingProvider(_token())
        with _CountingExecutor() as executor:
            orch, _, _ = _orchestrator(
                config,
                TokenStore(config.store),
                factory=lambda server, secret: stub,
                executor=executor,
            )
            await orch.begin_login("alice", "discovery-idp")
            assert executor.submitted == 2

    async def test_callback_exchanges_in_executor_and_caches_the_secret(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first flow submits resolve + exchange; the second only exchanges."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)

        def factory(server: ServerConfig, secret: str) -> OAuth2Provider:
            return _RecordingProvider(_token())

        with _CountingExecutor() as executor:
            async with TokenStore(config.store) as tokens:
                orch, _, _ = _orchestrator(config, tokens, factory=factory, executor=executor)
                url = await orch.begin_login("alice", "manual-idp")
                await orch.complete_callback("alice", _url_param(url, "state"), "c-1")
                assert executor.submitted == 2
                url2 = await orch.begin_login("alice", "manual-idp")
                await orch.complete_callback("alice", _url_param(url2, "state"), "c-2")
                assert executor.submitted == 3

    async def test_refresh_runs_in_executor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refresh submits the secret read and the provider round-trip."""
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        provider = _RefreshingProvider(_fresh_token(refresh="rt-stored"))
        with _CountingExecutor() as executor:
            async with TokenStore(config.store) as tokens:
                await tokens.save("alice", "manual-idp", "rt-stored")
                orch, _, _ = _orchestrator(
                    config, tokens, factory=lambda server, secret: provider, executor=executor
                )
                result = await orch.get_access_token("alice", "manual-idp")
                assert result.value == "at-fresh"
                assert executor.submitted == 2

    async def test_concurrent_secret_misses_resolve_twice_and_agree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two coroutines missing the secret cache resolve twice, identically.

        Pins the one accepted race: both callers miss the empty cache, both
        resolve the reference off-loop, and both loop-side writes carry the
        same deterministic value, so every flow sees a consistent secret.
        The gate holds the first resolution until the second has entered,
        which forces the double miss without any real sleep.
        """
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        both_entered = threading.Event()
        entries: list[str] = []
        entry_lock = threading.Lock()

        def gated_resolve(server: ServerConfig) -> str:
            with entry_lock:
                entries.append(server.name)
                if len(entries) == 2:
                    both_entered.set()
            # Liveness bound for a broken implementation, not a sleep: when
            # both misses happen as designed, the gate opens immediately.
            assert both_entered.wait(timeout=30.0)
            return resolve_client_secret(server)

        monkeypatch.setattr(orchestrator_module, "resolve_client_secret", gated_resolve)
        secrets_seen: list[str] = []

        def factory(server: ServerConfig, secret: str) -> OAuth2Provider:
            secrets_seen.append(secret)
            return _RefreshingProvider(_fresh_token(refresh="rt-stored"))

        with ThreadPoolExecutor(max_workers=2) as executor:
            async with TokenStore(config.store) as tokens:
                await tokens.save("alice", "manual-idp", "rt-stored")
                await tokens.save("bob", "manual-idp", "rt-stored")
                orch, _, _ = _orchestrator(config, tokens, factory=factory, executor=executor)
                first, second = await asyncio.gather(
                    orch.get_access_token("alice", "manual-idp"),
                    orch.get_access_token("bob", "manual-idp"),
                )
        assert entries == ["manual-idp", "manual-idp"]
        assert secrets_seen == [_SECRET, _SECRET]
        assert first.value == "at-fresh"
        assert second.value == "at-fresh"


class _FullFlowProvider(OAuth2Provider):
    """A no-network provider covering exchange, refresh, and revocation.

    Reused across every operation of one flow so a single instance can be
    inspected: it records the exchange (with its verifier), the refreshes,
    and the revocations, without ever touching the network.
    """

    def __init__(self, exchange: Token, refresh: Token) -> None:
        super().__init__(
            "stub",
            AuthProviderConfig(
                client_id="tessera-demo-client",
                client_secret=_SECRET,
                authorize_url=_AUTHORIZE_URL,
                token_url=_TOKEN_URL,
                revoke_url=f"{_ISSUER}/protocol/openid-connect/revoke",
                pkce=False,
            ),
            MemoryTokenStorage(),
        )
        self._exchange = exchange
        self._refresh = refresh
        self.exchanges: list[tuple[str, str | None]] = []
        self.refreshes: list[Token | None] = []
        self.revokes: list[Token | None] = []

    def exchange_code_stateless(self, code: str, *, code_verifier: str | None = None) -> Token:
        """Record the exchange arguments and return the scripted token."""
        self.exchanges.append((code, code_verifier))
        return self._exchange

    def refresh(self, token: Token | None = None) -> Token:
        """Record the refresh and return the scripted token."""
        self.refreshes.append(token)
        return self._refresh

    def revoke(self, token: Token | None = None, *, kinds: tuple[str, ...] | None = None) -> bool:
        """Record the revocation and confirm it."""
        self.revokes.append(token)
        return True


class TestLogTokenHygiene:
    """No token, secret, code, or verifier ever reaches a log, at any level."""

    async def test_no_secret_reaches_a_log_across_the_full_flow(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The full flow logs richly at DEBUG, yet leaks nothing sensitive.

        Drives every stage that touches secret material (login, callback,
        refresh, verify, revoke) at DEBUG and asserts two things: the flow
        is actually observed at DEBUG (the sanitized enrichment is present),
        and not one secret value appears anywhere in the captured logs.
        """
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        exchange = _token(
            refresh="rt-exchange-HYGIENE", scope=["openid"], id_token="id-token-HYGIENE"
        )
        rotated = _fresh_token(refresh="rt-rotated-HYGIENE", scope=["openid"])
        provider = _FullFlowProvider(exchange, rotated)
        async with TokenStore(config.store) as tokens:
            orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: provider)
            with caplog.at_level(logging.DEBUG):
                url = await orch.begin_login("alice", "manual-idp")
                state = _url_param(url, "state")
                await orch.complete_callback("alice", state, "auth-code-HYGIENE")
                await orch.get_access_token("alice", "manual-idp")  # rotates
                await orch.verify("alice", "manual-idp")  # keeps
                notified = await orch.revoke("alice", "manual-idp")
        assert notified is True
        verifier = provider.exchanges[0][1]
        assert verifier is not None
        text = caplog.text
        # The flow is observed at DEBUG: the sanitized enrichment is present.
        assert any(record.levelno == logging.DEBUG for record in caplog.records)
        assert "callback validated" in text
        # Not one secret value leaks, at any level.
        for secret in (
            state,
            verifier,
            "auth-code-HYGIENE",
            _SECRET,
            "rt-exchange-HYGIENE",
            "rt-rotated-HYGIENE",
            "id-token-HYGIENE",
            "at-1",
            "at-fresh",
        ):
            assert secret not in text

    async def test_real_configure_logging_pipeline_leaks_no_secret(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The production logging wiring (configure_logging) leaks nothing.

        The caplog sibling captures on the root logger with propagation on,
        which never exercises ``configure_logging``. This drives the same
        flow through the real wiring (a dedicated handler on the ``tessera``
        logger, propagation disabled, third-party loggers pinned) and
        captures on the tessera logger itself, so it measures exactly what a
        deployment logs.
        """
        monkeypatch.setenv(_ENV_NAME, _SECRET)
        config = _tessera_config(tmp_path)
        exchange = _token(refresh="rt-exchange-REAL", scope=["openid"], id_token="id-token-REAL")
        rotated = _fresh_token(refresh="rt-rotated-REAL", scope=["openid"])
        provider = _FullFlowProvider(exchange, rotated)
        managed = ("tessera", "httpx", "kstlib.config.loader", "kstlib.auth")
        snapshots = [
            (logger, list(logger.handlers), logger.level, logger.propagate)
            for logger in (logging.getLogger(name) for name in managed)
        ]
        buffer = io.StringIO()
        capture = logging.StreamHandler(buffer)
        try:
            configure_logging("DEBUG")
            logging.getLogger("tessera").addHandler(capture)
            async with TokenStore(config.store) as tokens:
                orch, _, _ = _orchestrator(config, tokens, factory=lambda server, secret: provider)
                url = await orch.begin_login("alice", "manual-idp")
                state = _url_param(url, "state")
                await orch.complete_callback("alice", state, "auth-code-REAL")
                await orch.get_access_token("alice", "manual-idp")  # rotates
                await orch.verify("alice", "manual-idp")  # keeps
                await orch.revoke("alice", "manual-idp")
        finally:
            for logger, handlers, level, propagate in snapshots:
                logger.handlers[:] = handlers
                logger.setLevel(level)
                logger.propagate = propagate
        verifier = provider.exchanges[0][1]
        assert verifier is not None
        output = buffer.getvalue()
        # The flow is observed on the real tessera pipeline (propagate disabled).
        assert "callback validated" in output
        # Not one secret value leaks, at any level.
        for secret in (
            state,
            verifier,
            "auth-code-REAL",
            _SECRET,
            "rt-exchange-REAL",
            "rt-rotated-REAL",
            "id-token-REAL",
            "at-1",
            "at-fresh",
        ):
            assert secret not in output
