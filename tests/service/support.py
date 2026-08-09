"""Shared harness for the tessera service endpoint tests.

Real handlers, real ``HubAuth``/``HubOAuth`` instances, and a local fake
Hub API serving ``GET /hub/api/user`` from a token table: the only
substituted element of the authentication stack is the Hub itself,
reached over genuine loopback HTTP. Identity providers are injected
through the orchestrator's provider factory, so no test touches any
network beyond that loopback.
"""

from __future__ import annotations

import os
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from jupyterhub.services.auth import HubAuth, HubOAuth
from kstlib.auth import (
    AuthProviderConfig,
    MemoryTokenStorage,
    OAuth2Provider,
    OIDCProvider,
    Token,
)
from kstlib.auth.errors import TokenExchangeError
from tornado.httpserver import HTTPServer
from tornado.testing import AsyncHTTPTestCase, bind_unused_port
from tornado.web import Application, RequestHandler, create_signed_value

from tessera.config.models import ServerConfig, ServiceConfig, StoreConfig, TesseraConfig
from tessera.flow.orchestrator import FlowOrchestrator
from tessera.flow.store import PendingFlowStore
from tessera.service.app import make_app
from tessera.store.store import TokenStore

CLIENT_SECRET = "a-very-confidential-client-secret-value"
SECRET_ENV_NAME = "TESSERA_TEST_SERVICE_SECRET"
PUBLIC_URL = "https://hub.example.com"
PREFIX = "/services/tessera/"
AUTHORIZE_URL = "https://idp.example.com/authorize"
TOKEN_URL = "https://idp.example.com/token"
ISSUER = "https://idp.example.com/realms/demo"

OAUTH_CLIENT_ID = "service-tessera"
SERVICE_API_TOKEN = "tessera-service-api-token"
ACCESS_SCOPE = "access:services!service=tessera"

ALICE_TOKEN = "alice-api-token"
BOB_TOKEN = "bob-api-token"
LOWPRIV_TOKEN = "lowpriv-api-token"
SERVICE_KIND_TOKEN = "robot-api-token"

USER_MODELS: dict[str, dict[str, Any]] = {
    ALICE_TOKEN: {"kind": "user", "name": "alice", "admin": False, "scopes": [ACCESS_SCOPE]},
    BOB_TOKEN: {"kind": "user", "name": "bob", "admin": False, "scopes": [ACCESS_SCOPE]},
    LOWPRIV_TOKEN: {"kind": "user", "name": "carol", "admin": False, "scopes": []},
    SERVICE_KIND_TOKEN: {
        "kind": "service",
        "name": "robot",
        "admin": False,
        "scopes": [ACCESS_SCOPE],
    },
}


def url_param(url: str, name: str) -> str:
    """Return a single query parameter from a URL.

    Args:
        url: The URL to parse.
        name: The parameter name.

    Returns:
        The parameter value.
    """
    return dict(parse_qsl(urlsplit(url).query))[name]


def build_config(base: Path) -> TesseraConfig:
    """Build a validated two-server configuration rooted at ``base``.

    Args:
        base: The directory holding the store key and database files.

    Returns:
        The validated configuration (explicit and discovery servers).
    """
    key_path = base / "db.key"
    key_path.write_text("a-strong-test-passphrase-for-the-store", encoding="utf-8")
    key_path.chmod(0o600)
    servers = {
        "manual-idp": ServerConfig.from_mapping(
            "manual-idp",
            {
                "provider": {
                    "authorization_endpoint": AUTHORIZE_URL,
                    "token_endpoint": TOKEN_URL,
                },
                "client_id": "tessera-demo-client",
                "client_secret_env": SECRET_ENV_NAME,
                "scopes": ["openid", "profile"],
            },
        ),
        "discovery-idp": ServerConfig.from_mapping(
            "discovery-idp",
            {
                "provider": {"issuer": ISSUER},
                "client_id": "tessera-demo-client",
                "client_secret_env": SECRET_ENV_NAME,
                "scopes": ["openid"],
            },
        ),
    }
    return TesseraConfig(
        servers=servers,
        store=StoreConfig.from_mapping(
            {"db_location": str(base / "tokens.db"), "key_file": str(key_path)}
        ),
        service=ServiceConfig.from_mapping({"public_url": PUBLIC_URL}),
    )


class FakeHubUserHandler(RequestHandler):
    """Serves ``GET /hub/api/user`` exactly like the Hub identity endpoint."""

    def initialize(self, models: dict[str, dict[str, Any]]) -> None:
        """Receive the token table.

        Args:
            models: The identity model served for each known token.
        """
        self._models = models

    def get(self) -> None:
        """Resolve the ``Authorization: token`` header against the table."""
        header = self.request.headers.get("Authorization", "")
        token = header.partition(" ")[2].strip() if header.lower().startswith("token") else ""
        model = self._models.get(token)
        if model is None:
            self.set_status(403)
            self.finish({"status": 403, "message": "Forbidden"})
            return
        self.finish(model)


class ProviderJournal:
    """Scriptable results and a call log shared across ephemeral providers.

    The orchestrator builds one provider per operation; the journal is
    the stable object a test scripts (results, optional refresh gate)
    and asserts against (recorded calls).
    """

    def __init__(self) -> None:
        """Start with a happy exchange, a rotation-free refresh, a notified revoke."""
        self.exchange_result: Token | Exception = Token(
            access_token="at-1", refresh_token="rt-1", scope=["openid"]
        )
        self.refresh_result: Token | Exception = Token(
            access_token="at-fresh", refresh_token=None, scope=["openid"]
        )
        self.revoke_result: bool = True
        self.exchanges: list[tuple[str, str | None]] = []
        self.refreshes: list[Token | None] = []
        self.revokes: list[Token | None] = []
        self.refresh_gate: threading.Event | None = None


class RecordingProvider(OAuth2Provider):
    """A no-network kstlib provider with scriptable exchange and refresh."""

    def __init__(self, journal: ProviderJournal) -> None:
        """Bind the provider to the shared journal.

        Args:
            journal: The journal scripting results and recording calls.
        """
        super().__init__(
            "stub",
            AuthProviderConfig(
                client_id="tessera-demo-client",
                authorize_url=AUTHORIZE_URL,
                token_url=TOKEN_URL,
                pkce=False,
            ),
            MemoryTokenStorage(),
        )
        self._journal = journal

    def exchange_code_stateless(self, code: str, *, code_verifier: str | None = None) -> Token:
        """Record the exchange and return (or raise) the scripted result."""
        self._journal.exchanges.append((code, code_verifier))
        if isinstance(self._journal.exchange_result, Exception):
            raise self._journal.exchange_result
        return self._journal.exchange_result

    def refresh(self, token: Token | None = None) -> Token:
        """Record the refresh, honor the gate, return or raise the result."""
        self._journal.refreshes.append(token)
        gate = self._journal.refresh_gate
        if gate is not None:
            # Liveness bound for a broken implementation, not a sleep: the
            # test opens the gate as soon as its precondition is met.
            assert gate.wait(timeout=30.0)
        if isinstance(self._journal.refresh_result, Exception):
            raise self._journal.refresh_result
        return self._journal.refresh_result

    def revoke(self, token: Token | None = None, *, kinds: tuple[str, ...] | None = None) -> bool:
        """Record the revocation and return the scripted outcome."""
        self._journal.revokes.append(token)
        return self._journal.revoke_result


class FailingDiscoveryProvider(OIDCProvider):
    """An OIDC provider whose discovery always fails, without network."""

    def __init__(self) -> None:
        """Build a discovery-mode provider for the stub issuer."""
        super().__init__(
            "stub",
            AuthProviderConfig(
                client_id="tessera-demo-client",
                issuer=ISSUER,
                pkce=False,
            ),
            MemoryTokenStorage(),
        )

    def discover(self, *, force: bool = False) -> dict[str, Any]:
        """Fail exactly like an unreachable discovery document."""
        raise TokenExchangeError("discovery unavailable")


class ServiceTestCase(AsyncHTTPTestCase):
    """Base test case assembling the real service over a fake Hub API.

    Subclasses tune the assembly through ``pending_max_size``, by
    overriding ``provider_factory`` (per-operation providers) or
    ``build_app`` (custom routing), and script provider behavior through
    ``self.journal``.
    """

    pending_max_size = 100

    def get_app(self) -> Application:
        """Assemble the fake Hub, the orchestrator, and the service app."""
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        os.environ[SECRET_ENV_NAME] = CLIENT_SECRET
        self.addCleanup(os.environ.pop, SECRET_ENV_NAME, None)
        self.config = build_config(base)
        sock, port = bind_unused_port()
        self.user_models = {key: dict(value) for key, value in USER_MODELS.items()}
        self.fake_hub = HTTPServer(
            Application([(r"/hub/api/user", FakeHubUserHandler, {"models": self.user_models})])
        )
        self.fake_hub.add_sockets([sock])
        hub_api_url = f"http://127.0.0.1:{port}/hub/api"
        self.hub_auth = HubAuth(
            api_url=hub_api_url,
            api_token=SERVICE_API_TOKEN,
            access_scopes={ACCESS_SCOPE},
            base_url=PREFIX,
        )
        self.hub_oauth = HubOAuth(
            api_url=hub_api_url,
            api_token=SERVICE_API_TOKEN,
            oauth_client_id=OAUTH_CLIENT_ID,
            access_scopes={ACCESS_SCOPE},
            base_url=PREFIX,
        )
        self.store = TokenStore(self.config.store)
        self.io_loop.run_sync(self.store.initialize)
        self.journal = ProviderJournal()
        self.pending = PendingFlowStore(max_size=self.pending_max_size)
        self.orchestrator = FlowOrchestrator(
            self.config, self.pending, self.store, provider_factory=self.provider_factory
        )
        self.cookie_secret = secrets.token_bytes(32)
        return self.build_app()

    def tearDown(self) -> None:
        """Close the store and the fake Hub while the loop is still alive."""
        self.io_loop.run_sync(self.store.close)
        self.fake_hub.stop()
        super().tearDown()

    def build_app(self) -> Application:
        """Build the application under test; override for custom routing."""
        return make_app(
            self.orchestrator,
            prefix=PREFIX,
            hub_auth=self.hub_auth,
            hub_oauth=self.hub_oauth,
            cookie_secret=self.cookie_secret,
        )

    def provider_factory(self, server: ServerConfig, secret: str) -> OAuth2Provider:
        """Build the journal-bound provider; override for failure modes."""
        return RecordingProvider(self.journal)

    def service_url(self, path: str, server: str | None = None) -> str:
        """Return a path under the service prefix, with an optional server.

        Args:
            path: The route name (``login``, ``callback``, ...).
            server: When given, appended as the ``server`` query parameter.

        Returns:
            The request path.
        """
        url = f"{PREFIX}{path}"
        if server is not None:
            url = f"{url}?server={server}"
        return url

    def token_headers(self, token: str = ALICE_TOKEN) -> dict[str, str]:
        """Return API-token authentication headers.

        Args:
            token: The JupyterHub API token to present.

        Returns:
            The headers mapping.
        """
        return {"Authorization": f"token {token}"}

    def cookie_headers(self, token: str = ALICE_TOKEN) -> dict[str, str]:
        """Return browser-session headers: the forged Hub OAuth cookie.

        The cookie value is signed with the application's own cookie
        secret, exactly as the Hub OAuth callback would have stored it.

        Args:
            token: The JupyterHub API token the session carries.

        Returns:
            The headers mapping.
        """
        signed = create_signed_value(self.cookie_secret, OAUTH_CLIENT_ID, token)
        return {"Cookie": f"{OAUTH_CLIENT_ID}={signed.decode('ascii')}"}

    def seed_token(
        self,
        username: str = "alice",
        server: str = "manual-idp",
        refresh_token: str = "rt-stored",
        expires_at: float | None = None,
        scope: str | None = None,
        refresh_expires_at: float | None = None,
    ) -> None:
        """Store a refresh token directly, as a completed login would.

        Args:
            username: The owning Hub user.
            server: The declared server name.
            refresh_token: The refresh token value to store.
            expires_at: The indicative expiry to store, if any.
            scope: The granted scope string to store, if any.
            refresh_expires_at: The refresh-token expiry to store, if any.
        """

        async def _save() -> None:
            await self.store.save(
                username,
                server,
                refresh_token,
                expires_at=expires_at,
                scope=scope,
                refresh_expires_at=refresh_expires_at,
            )

        self.io_loop.run_sync(_save)

    def begin_login(self, headers: dict[str, str], server: str = "manual-idp") -> str:
        """Run ``/login`` through HTTP and return the issued state.

        Args:
            headers: The authentication headers to use.
            server: The declared server name to log in against.

        Returns:
            The ``state`` parameter of the identity provider redirect.
        """
        response = self.fetch(
            self.service_url("login", server=server), headers=headers, follow_redirects=False
        )
        assert response.code == 302
        return url_param(response.headers["Location"], "state")
