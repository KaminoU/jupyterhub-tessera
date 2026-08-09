"""OAuth authorization-code flow orchestration on top of kstlib.auth.

The orchestrator owns the multi-user flow state: it generates the state
and PKCE material (:mod:`tessera.flow.crypto`), registers one-shot pending
flows bound to a user, and validates the callback before anything else
happens. kstlib.auth is used only for the stateless pieces: OIDC discovery
and the back-channel code exchange, through an ephemeral provider that is
built per operation and always closed afterwards.

The kstlib provider configuration is built fresh for every operation:
kstlib provider constructors write issuer-derived placeholder endpoints
into the configuration object they receive, so a shared object would
carry one construction's mutations into the next. Only the resolved
client secret is cached for the lifetime of the process, so rotating a
client secret takes effect after a service restart.

kstlib's provider calls are synchronous, so every blocking operation
(secret resolution, OIDC discovery, code exchange, token refresh) runs in
an executor and never blocks the event loop. All mutable state (the
pending flows, the single-flight map, the per-server secret cache, the
token store) is read and written on the event loop only: a blocking helper
receives a fully built provider (construction is network-free), performs
the kstlib calls, closes the provider, and returns the result. The one
deliberate exception is the resolved-secret cache: two coroutines missing
it concurrently both resolve the reference off-loop and both write the
same deterministic value back on the loop, a benign double read.

After a login, :meth:`FlowOrchestrator.get_access_token` turns the stored
refresh token into a fresh access token on every call (no service-side
cache: smoothing is the kernel client's job). Concurrent calls for one
(user, server) pair coalesce into a single provider refresh; every
successful refresh rewrites the record with the response's fresh metadata
(the refresh window of a session-bound token slides on each refresh), a
rotated refresh token atomically replacing the stored one; a definitive
provider rejection deletes the record so the status turns red and the user
signs in again.

Security invariants:
    - The redirect URI always comes from the service configuration, never
      from a request parameter.
    - A callback is accepted only when its state consumes a pending flow
      (one-shot, TTL-bound) that was registered for the same username.
    - No state, code, verifier, token, or client secret value ever appears
      in a log or an error message. kstlib config and provider objects are
      never logged or repr-ed either: upstream keeps the client secret in
      their repr.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar, cast
from urllib.parse import urlencode, urlsplit, urlunsplit

from kstlib.auth import (
    AuthProviderConfig,
    MemoryTokenStorage,
    OAuth2Provider,
    OIDCProvider,
    Token,
)
from kstlib.auth.errors import AuthError, TokenExchangeError, TokenRefreshError

from tessera._shared.urls import REASON_INSECURE_SCHEME, UrlValidationError, validate_url
from tessera.config.models import ServerConfig
from tessera.flow.crypto import (
    compute_code_challenge,
    generate_code_verifier,
    generate_state,
)
from tessera.flow.errors import (
    CallbackValidationError,
    ExchangeError,
    ExchangeRejectedError,
    NoStoredTokenError,
    RefreshRejectedError,
    SecretResolutionError,
    UnknownServerError,
)
from tessera.flow.models import AccessToken, PendingFlow, ServerInfo, TokenStatus
from tessera.flow.secrets import resolve_client_secret

if TYPE_CHECKING:
    from concurrent.futures import Executor

    from tessera.config.models import TesseraConfig
    from tessera.flow.store import PendingFlowStore
    from tessera.store.models import TokenRecord
    from tessera.store.store import TokenStore

log = logging.getLogger(__name__)

MAX_STATE_LEN = 512
MAX_CODE_LEN = 2048

ProviderFactory = Callable[[ServerConfig, str], OAuth2Provider]
"""Builds the provider for one operation from a server and its resolved secret."""

_T = TypeVar("_T")


def _expiry_of(token: Token) -> float | None:
    """Return the Unix expiry of a kstlib token's access token, if known.

    Args:
        token: The kstlib token to read.

    Returns:
        The expiry as Unix seconds, or None when the provider gave none.
    """
    return token.expires_at.timestamp() if token.expires_at is not None else None


def _scope_of(token: Token) -> str | None:
    """Return the space-joined granted scope of a kstlib token, if any.

    Args:
        token: The kstlib token to read.

    Returns:
        The scope string, or None when the provider granted no scope list.
    """
    return " ".join(token.scope) if token.scope else None


def _refresh_expiry_of(token: Token, now: float) -> float | None:
    """Return the dated expiry of a token's refresh token, if communicated.

    ``refresh_expires_in`` is a non-standard field, so kstlib parks it in
    the token metadata. Keycloak always sends it: a positive number of
    seconds for a session-bound refresh token, and ``0`` for an offline
    token, meaning no expiry is communicated. Anything that is not a
    positive number maps to None: the expiry is never extrapolated.

    Args:
        token: The kstlib token to read.
        now: The current Unix time in seconds (the orchestrator's clock).

    Returns:
        The refresh-token expiry as Unix seconds, or None.
    """
    value = token.metadata.get("refresh_expires_in")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return now + float(value)


def _refresh_kind_of(scope: str | None) -> str:
    """Derive the refresh-token kind from a stored scope string.

    Args:
        scope: The granted scope stored with the record, if any.

    Returns:
        ``"offline"`` when ``offline_access`` was granted, ``"session"``
        when a scope was granted without it, ``"unknown"`` otherwise.
    """
    if scope is None:
        return "unknown"
    return "offline" if "offline_access" in scope.split() else "session"


def _with_query(endpoint: str, params: dict[str, str]) -> str:
    """Add query parameters to an endpoint, keeping any it already carries.

    RFC 6749 section 3.1 allows the authorization endpoint to include a query
    component, which the client must retain: a multi-tenant provider
    publishing ``.../authorize?tenant=acme`` stays reachable only if its own
    parameters survive. Appending with a second ``?`` would produce a URL the
    provider cannot parse.

    Args:
        endpoint: The authorization endpoint, with or without a query.
        params: The flow parameters to add.

    Returns:
        The endpoint carrying its original query followed by the parameters.

    Examples:
        >>> _with_query("https://idp.example.com/authorize?tenant=acme", {"state": "s"})
        'https://idp.example.com/authorize?tenant=acme&state=s'
    """
    parts = urlsplit(endpoint)
    query = urlencode(params)
    if parts.query:
        query = f"{parts.query}&{query}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _discover_blocking(provider: OAuth2Provider) -> str:
    """Resolve the authorization endpoint of a built provider (blocking).

    Runs in the executor: it performs the OIDC discovery network round-trip
    and always closes the provider. It touches no orchestrator state. The
    endpoint is taken from the discovery document itself, never from the
    endpoint attributes of the configuration object: the document is what
    the provider actually served for this operation.

    Args:
        provider: The fully built, single-operation provider.

    Returns:
        The authorization endpoint URL.

    Raises:
        AuthError: If the discovery interaction fails (mapped by the caller).
        ExchangeError: If a completed discovery serves no usable
            authorization endpoint, or one that is not an acceptable URL.
    """
    try:
        if isinstance(provider, OIDCProvider):
            document = provider.discover()
            endpoint = document.get("authorization_endpoint")
            if not isinstance(endpoint, str) or not endpoint:
                log.warning(
                    "discovery for server '%s' returned no usable authorization_endpoint",
                    provider.name,
                )
                raise ExchangeError(
                    f"server '{provider.name}': the discovery document has no usable "
                    "authorization_endpoint; check the provider's "
                    ".well-known/openid-configuration document"
                )
            try:
                return validate_url(endpoint)
            except UrlValidationError as exc:
                nature = "a non-TLS" if exc.reason == REASON_INSECURE_SCHEME else "an unusable"
                log.warning(
                    "discovery for server '%s' announced %s authorization_endpoint, rejected",
                    provider.name,
                    nature,
                )
                raise ExchangeError(
                    f"server '{provider.name}': the discovery document announced {nature} "
                    "authorization_endpoint; check the provider's "
                    ".well-known/openid-configuration document"
                ) from exc
        # cast: in explicit mode the validated configuration carries the
        # authorization endpoint the provider was built from.
        return cast("str", provider.config.authorize_url)
    finally:
        provider.close()


def _exchange_blocking(provider: OAuth2Provider, code: str, code_verifier: str) -> Token:
    """Exchange an authorization code through a built provider (blocking).

    Runs in the executor and always closes the provider. The stateless
    exchange is built for exactly this consumer shape: tessera validates
    its own CSRF state in its pending store and sends its own PKCE
    verifier, so no provider-side flow state exists to check. In discovery
    mode the provider resolves the real token endpoint itself before
    posting, and the returned token is not persisted provider-side:
    tessera's encrypted store is the only persistence.

    Args:
        provider: The fully built, single-operation provider.
        code: The authorization code to exchange.
        code_verifier: tessera's PKCE verifier for this flow.

    Returns:
        The kstlib token returned by the provider.

    Raises:
        AuthError: If the provider interaction fails (mapped by the caller).
    """
    try:
        return provider.exchange_code_stateless(code, code_verifier=code_verifier)
    finally:
        provider.close()


def _refresh_blocking(provider: OAuth2Provider, refresh_token: str) -> Token:
    """Refresh an access token through a built provider (blocking).

    Runs in the executor and always closes the provider. The empty access
    token is a placeholder: tessera never stores access tokens and kstlib's
    refresh reads only ``refresh_token``.

    Args:
        provider: The fully built, single-operation provider.
        refresh_token: The stored refresh token to send.

    Returns:
        The fresh kstlib token returned by the provider.

    Raises:
        AuthError: If the refresh interaction fails (mapped by the caller).
    """
    try:
        return provider.refresh(token=Token(access_token="", refresh_token=refresh_token))
    finally:
        provider.close()


def _revoke_blocking(provider: OAuth2Provider, refresh_token: str) -> bool:
    """Revoke a stored refresh token through a built provider (blocking).

    Runs in the executor and always closes the provider. In discovery
    mode the revocation endpoint only exists after discovery (kstlib's
    base ``revoke`` reads its configured URL directly), so the document
    is fetched first, mirroring kstlib's own discover-then-refresh.

    The revocation targets the refresh token alone: tessera never stores
    an access token, and the targeted kind keeps the reported success
    meaningful for the one revocation that matters (RFC 7009 answers 200
    even for unknown tokens, so an untargeted extra post would earn its
    own 200 and dilute the verdict).

    Args:
        provider: The fully built, single-operation provider.
        refresh_token: The stored refresh token to revoke.

    Returns:
        True when the provider confirmed the revocation, False when no
        revocation endpoint is known or the call failed.

    Raises:
        AuthError: If the discovery interaction fails (mapped by the caller).
    """
    try:
        if isinstance(provider, OIDCProvider):
            provider.discover()
        token = Token(access_token="", refresh_token=refresh_token)
        return provider.revoke(token=token, kinds=("refresh_token",))
    finally:
        provider.close()


class FlowOrchestrator:
    """Runs the authorization-code flow for many concurrent users.

    :meth:`begin_login` produces the authorization URL for a (user, server)
    pair and registers the matching one-shot pending flow;
    :meth:`complete_callback` validates the returning state, exchanges the
    code through kstlib, and persists the refresh token;
    :meth:`get_access_token` turns the stored refresh token into a fresh
    access token, handling rotation and reuse detection;
    :meth:`get_status` is the cheap stored-token status polled by the
    panel, and :meth:`list_servers` its static button topology. Build one
    orchestrator per service process.
    """

    def __init__(
        self,
        config: TesseraConfig,
        pending: PendingFlowStore,
        tokens: TokenStore,
        *,
        provider_factory: ProviderFactory | None = None,
        clock: Callable[[], float] = time.time,
        executor: Executor | None = None,
    ) -> None:
        """Assemble the orchestrator from its collaborators.

        Args:
            config: The validated tessera configuration.
            pending: The bounded store of in-flight flows.
            tokens: The encrypted refresh-token store.
            provider_factory: Optional factory building the kstlib provider
                for one operation, given the server and its resolved client
                secret. Defaults to the real kstlib providers; injectable so
                tests never touch the network.
            clock: Callable returning the current Unix time in seconds.
                Injected so tests can control flow expiry without sleeping.
            executor: Executor running the blocking kstlib operations
                (secret resolution, discovery, code exchange, token
                refresh). None uses the event loop's default thread pool;
                the service process passes a dedicated bounded pool it owns
                and shuts down.
        """
        self._config = config
        self._pending = pending
        self._tokens = tokens
        self._provider_factory = provider_factory
        self._clock = clock
        self._executor = executor
        self._secrets: dict[str, str] = {}
        # Single-flight map of in-progress refreshes. An entry lives exactly
        # as long as its refresh operation (the done callback removes it on
        # every terminal state), so the map size is bounded by the number of
        # concurrently refreshing pairs and is empty at rest.
        self._inflight: dict[tuple[str, str], asyncio.Task[AccessToken]] = {}

    async def begin_login(self, username: str, provider_name: str) -> str:
        """Start a login flow and return the provider authorization URL.

        tessera builds the URL itself: the state and the S256 challenge of a
        fresh PKCE verifier are its own, and the redirect URI comes from the
        service configuration. Discovery, when needed, runs in the executor
        first; the crypto material is then generated on the event loop and
        the pending flow is registered last, so a failed discovery leaves
        no orphan entry.

        Args:
            username: The authenticated JupyterHub user starting the flow.
            provider_name: The declared server name to authenticate against.

        Returns:
            The authorization URL to redirect the user to.

        Raises:
            UnknownServerError: If no server with that name is declared.
            SecretResolutionError: If the client secret cannot be resolved
                (discovery mode resolves it eagerly, before any redirect).
            ExchangeError: If OIDC discovery fails.
            PendingFlowStoreFull: If too many flows are already in flight.
        """
        server = self._server(provider_name)
        endpoint = await self._authorization_endpoint(server)
        state = generate_state()
        code_verifier = generate_code_verifier()
        params = {
            "response_type": "code",
            "client_id": server.client_id,
            "redirect_uri": self._config.service.redirect_uri,
            "scope": " ".join(server.scopes),
            "state": state,
            "code_challenge": compute_code_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
        url = _with_query(endpoint, params)
        flow = PendingFlow(
            username=username,
            provider=provider_name,
            code_verifier=code_verifier,
            created_at=self._clock(),
        )
        self._pending.add(state, flow)
        log.debug("login flow started for user=%s server=%s", username, provider_name)
        return url

    async def complete_callback(self, username: str, state: str, code: str) -> TokenRecord:
        """Validate an OAuth callback, exchange the code, store the token.

        Validation happens before any provider interaction: size hardening,
        then the one-shot state lookup, then the user binding. Every
        rejection is logged with a categorized ``[SECURITY]`` line that never
        carries the received values.

        Args:
            username: The authenticated JupyterHub user returning from the
                provider.
            state: The ``state`` query parameter of the callback.
            code: The authorization code of the callback.

        Returns:
            The stored token record.

        Raises:
            CallbackValidationError: If the state is missing, oversized,
                unknown, expired, replayed, or bound to another user, or if
                the code is missing or oversized.
            UnknownServerError: If the flow references a server that is no
                longer declared.
            SecretResolutionError: If the client secret cannot be resolved.
            ExchangeRejectedError: If the provider answered the exchange
                with a definitive OAuth error.
            ExchangeError: If the exchange interaction fails or the
                provider returns no refresh token.
        """
        if not state or len(state) > MAX_STATE_LEN:
            log.warning("[SECURITY] callback state missing or oversized, rejected")
            raise CallbackValidationError("invalid state")
        if not code or len(code) > MAX_CODE_LEN:
            log.warning("[SECURITY] callback code missing or oversized, rejected")
            raise CallbackValidationError("invalid authorization code")
        flow = self._pending.consume(state)
        if flow is None:
            log.warning("[SECURITY] callback state unknown, expired, or replayed, rejected")
            raise CallbackValidationError("invalid state")
        if flow.username != username:
            log.warning("[SECURITY] callback state bound to another user, rejected")
            raise CallbackValidationError("invalid state")
        log.debug("callback validated for user=%s server=%s", username, flow.provider)
        server = self._server(flow.provider)
        token = await self._exchange(server, code, flow.code_verifier)
        if token.refresh_token is None:
            raise ExchangeError(f"server '{server.name}' returned no refresh token")
        record = await self._tokens.save(
            username,
            flow.provider,
            token.refresh_token,
            expires_at=_expiry_of(token),
            scope=_scope_of(token),
            refresh_expires_at=_refresh_expiry_of(token, self._clock()),
        )
        log.info("stored refresh token for user=%s server=%s", username, flow.provider)
        return record

    async def get_access_token(self, username: str, provider_name: str) -> AccessToken:
        """Return a fresh access token for a (user, server) pair.

        Every call refreshes against the provider (no service-side cache:
        traffic smoothing belongs to the kernel client). Concurrent calls
        for the same pair coalesce into one shared refresh: they all await
        the same in-flight operation and receive its result, or its error,
        together. A caller landing between the operation's completion and
        its removal receives the just-completed result.

        Args:
            username: The authenticated JupyterHub user.
            provider_name: The declared server name to get a token for.

        Returns:
            The fresh access token with its expiry and granted scope.

        Raises:
            UnknownServerError: If no server with that name is declared.
            NoStoredTokenError: If no refresh token is stored for the pair.
            SecretResolutionError: If the client secret cannot be resolved.
            RefreshRejectedError: If the provider definitively rejected the
                refresh token; the stored record has been deleted.
            ExchangeError: If the refresh failed transiently (network,
                provider 5xx); the stored record is kept.
        """
        key = (username, provider_name)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._refresh_access_token(username, provider_name))
            self._inflight[key] = task
            task.add_done_callback(lambda done: self._discard_inflight(key, done))
        return await task

    async def get_status(self, username: str, provider_name: str) -> TokenStatus:
        """Report the stored-token status for a (user, server) pair.

        Cheap by design (one store read, no provider interaction) so the
        frontend can poll it. A stored record means "presumed valid": the
        reported fields are the ones observed at the last store write;
        actual invalidity is only discovered (and the record deleted) by
        the next :meth:`get_access_token`. The refresh kind is derived
        from the stored scope at response time, so it costs no column and
        cannot drift from the scope it derives from.

        Args:
            username: The authenticated JupyterHub user.
            provider_name: The declared server name to report on.

        Returns:
            The panel-facing status: token presence and its detail fields.

        Raises:
            UnknownServerError: If no server with that name is declared.
        """
        self._server(provider_name)
        record = await self._tokens.load(username, provider_name)
        if record is None:
            return TokenStatus(
                has_token=False,
                expires_at=None,
                created_at=None,
                updated_at=None,
                scope=None,
                refresh_kind="unknown",
                refresh_expires_at=None,
            )
        return TokenStatus(
            has_token=True,
            expires_at=record.expires_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
            scope=record.scope,
            refresh_kind=_refresh_kind_of(record.scope),
            refresh_expires_at=record.refresh_expires_at,
        )

    def list_servers(self) -> tuple[ServerInfo, ...]:
        """Return the declared servers' button descriptors, in declaration order.

        A pure configuration read (no I/O, no store access): the panel
        loads it once at startup to render one button per server. Nothing
        secret or provider-facing leaves this method.

        Returns:
            One :class:`~tessera.flow.models.ServerInfo` per declared
            server, in the order of the configuration file.
        """
        return tuple(
            ServerInfo(
                name=server.name,
                label=server.button.label,
                color_valid=server.button.color_valid,
                color_invalid=server.button.color_invalid,
            )
            for server in self._config.servers.values()
        )

    async def verify(self, username: str, provider_name: str) -> bool:
        """Test the stored token against the provider and answer a boolean.

        Delegates to :meth:`get_access_token`, so this is the same real
        refresh round-trip (coalesced with any concurrent one: a
        double-click costs one provider call) and only the verdict leaves.
        A definitive provider rejection IS the answer False, not an
        error: the refresh path has already purged the record, so the
        next status poll turns red. Anything short of a definitive
        verdict propagates instead: a transient failure never fabricates
        a False. On a provider that rotates refresh tokens, a successful
        verify consumes a real refresh and persists the rotated token,
        exactly like any other refresh.

        Args:
            username: The authenticated JupyterHub user.
            provider_name: The declared server name to verify against.

        Returns:
            True when the provider accepted the stored refresh token,
            False when it definitively rejected it.

        Raises:
            UnknownServerError: If no server with that name is declared.
            NoStoredTokenError: If no refresh token is stored for the pair.
            SecretResolutionError: If the client secret cannot be resolved.
            ExchangeError: If the refresh failed transiently (the stored
                record is kept: no verdict was reached).
        """
        try:
            await self.get_access_token(username, provider_name)
        except RefreshRejectedError:
            return False
        return True

    async def revoke(self, username: str, provider_name: str) -> bool:
        """Revoke the stored token: provider best-effort, local purge always.

        The order is deliberate: the provider is notified BEFORE the
        local purge, because a purged-first record would leave an
        orphaned refresh token, still valid at the provider yet
        definitively irrevocable from here. The provider call is
        best-effort: no known revocation endpoint, an unresolvable
        client secret, or a failed interaction all leave the purge to
        proceed and are reported honestly in the returned flag. The
        deliberate action is logged at INFO, never with a token value.

        Args:
            username: The authenticated JupyterHub user.
            provider_name: The declared server name to revoke against.

        Returns:
            True when the provider confirmed the revocation, False when
            it could not be notified (the local purge happened anyway).

        Raises:
            UnknownServerError: If no server with that name is declared.
            NoStoredTokenError: If no refresh token is stored for the pair.
        """
        server = self._server(provider_name)
        record = await self._tokens.load(username, provider_name)
        if record is None:
            raise NoStoredTokenError(
                f"no token stored for server '{server.name}': nothing to revoke"
            )
        notified = False
        try:
            provider = await self._build_provider(server)
            notified = await self._run_blocking(_revoke_blocking, provider, record.refresh_token)
        except SecretResolutionError:
            log.warning(
                "provider revocation skipped for server '%s' (client secret unresolved)",
                server.name,
            )
        except AuthError:
            log.warning("provider revocation failed for server '%s'", server.name)
        await self._tokens.delete(username, provider_name)
        log.info(
            "revoked stored token for user=%s server=%s provider_notified=%s",
            username,
            provider_name,
            notified,
        )
        return notified

    def _discard_inflight(self, key: tuple[str, str], task: asyncio.Task[AccessToken]) -> None:
        """Drop a finished refresh from the single-flight map.

        Registered as the task's done callback, so it runs on every
        terminal state and keeps the map bounded. The exception, if any, is
        read here so a refresh whose awaiters were all cancelled does not
        trigger asyncio's never-retrieved warning.

        Args:
            key: The (username, provider) pair the task refreshed.
            task: The finished refresh task.
        """
        self._inflight.pop(key, None)
        if not task.cancelled():
            task.exception()

    async def _refresh_access_token(self, username: str, provider_name: str) -> AccessToken:
        """Run one coalesced refresh for a (user, server) pair.

        The stored refresh token is sent through an ephemeral provider.
        Every successful refresh rewrites the record with the response's
        fresh metadata (expiry, scope, refresh expiry: the refresh window
        of a session-bound token slides on each refresh), the upsert
        preserving ``created_at``; a rotated refresh token replaces the
        stored one in that same atomic write, while an absent one keeps
        the stored value. A definitive rejection deletes the record.
        kstlib reports transient and definitive refresh failures with the
        same exception type; its ``retryable`` flag tells them apart, and
        only a definitive rejection invalidates the record.

        Args:
            username: The authenticated JupyterHub user.
            provider_name: The declared server name to refresh against.

        Returns:
            The fresh access token with its expiry and granted scope.

        Raises:
            UnknownServerError: If no server with that name is declared.
            NoStoredTokenError: If no refresh token is stored for the pair.
            SecretResolutionError: If the client secret cannot be resolved.
            RefreshRejectedError: If the provider definitively rejected the
                refresh token; the record is deleted before raising.
            ExchangeError: If the refresh failed transiently.
        """
        server = self._server(provider_name)
        record = await self._tokens.load(username, provider_name)
        if record is None:
            raise NoStoredTokenError(
                f"no token stored for server '{server.name}': "
                "sign in with the tessera button in JupyterLab"
            )
        provider = await self._build_provider(server)
        try:
            token = await self._run_blocking(_refresh_blocking, provider, record.refresh_token)
        except TokenRefreshError as exc:
            if exc.retryable:
                log.warning(
                    "token refresh unavailable for server '%s' (transient failure)",
                    server.name,
                )
                raise ExchangeError(f"token refresh failed for server '{server.name}'") from exc
            await self._tokens.delete(username, provider_name)
            log.warning(
                "[SECURITY] refresh rejected by server '%s' for user=%s, stored token invalidated",
                server.name,
                username,
            )
            raise RefreshRejectedError(
                f"server '{server.name}' rejected the refresh token: sign in again"
            ) from exc
        except AuthError as exc:
            log.warning("token refresh failed for server '%s'", server.name)
            raise ExchangeError(f"token refresh failed for server '{server.name}'") from exc
        new_refresh = token.refresh_token
        await self._tokens.save(
            username,
            provider_name,
            new_refresh if new_refresh is not None else record.refresh_token,
            expires_at=_expiry_of(token),
            scope=_scope_of(token),
            refresh_expires_at=_refresh_expiry_of(token, self._clock()),
        )
        if new_refresh is not None and new_refresh != record.refresh_token:
            log.info("rotated refresh token for user=%s server=%s", username, provider_name)
        else:
            log.debug("refresh kept stored token for user=%s server=%s", username, provider_name)
        return AccessToken(
            value=token.access_token,
            expires_at=_expiry_of(token),
            scope=_scope_of(token),
        )

    def _server(self, provider_name: str) -> ServerConfig:
        """Resolve a declared server or raise a typed error.

        Args:
            provider_name: The requested server name.

        Returns:
            The matching server configuration.

        Raises:
            UnknownServerError: If no server with that name is declared.
        """
        try:
            return self._config.get(provider_name)
        except KeyError as exc:
            raise UnknownServerError(f"unknown server '{provider_name[:64]}'") from exc

    async def _authorization_endpoint(self, server: ServerConfig) -> str:
        """Return the authorization endpoint, discovering it if needed.

        In explicit mode the endpoint comes straight from the validated
        configuration, without touching the executor. In discovery mode an
        ephemeral provider is built on the event loop, then fetches the
        discovery document in the executor and is closed there.

        Args:
            server: The server to resolve the endpoint for.

        Returns:
            The authorization endpoint URL.

        Raises:
            ExchangeError: If OIDC discovery fails.
            SecretResolutionError: If the client secret cannot be resolved.
        """
        endpoint = server.provider.authorization_endpoint
        if endpoint is not None:
            return endpoint
        provider = await self._build_provider(server)
        try:
            return await self._run_blocking(_discover_blocking, provider)
        except AuthError as exc:
            log.warning("discovery failed for server '%s'", server.name)
            raise ExchangeError(f"discovery failed for server '{server.name}'") from exc

    async def _exchange(self, server: ServerConfig, code: str, code_verifier: str) -> Token:
        """Exchange an authorization code through an ephemeral provider.

        The provider is built on the event loop; the network round-trip
        (:func:`_exchange_blocking`) runs in the executor, which also closes
        the provider. Error mapping and logging happen back on the loop,
        on the structured kstlib contract: a ``TokenExchangeError`` whose
        ``status_code`` is set and whose ``retryable`` flag is off is a
        definitive provider rejection (a 4xx answer: authorization codes
        are single-use, retrying cannot help). Everything else is a plain
        exchange failure: a transport error, a local kstlib guard, or a
        provider 5xx, where retrying the same exchange may succeed.

        Args:
            server: The server the code belongs to.
            code: The authorization code to exchange.
            code_verifier: tessera's PKCE verifier for this flow.

        Returns:
            The kstlib token returned by the provider.

        Raises:
            ExchangeRejectedError: If the provider answered the exchange
                with a definitive OAuth error.
            ExchangeError: If the provider interaction fails.
            SecretResolutionError: If the client secret cannot be resolved.
        """
        provider = await self._build_provider(server)
        try:
            return await self._run_blocking(_exchange_blocking, provider, code, code_verifier)
        except TokenExchangeError as exc:
            if exc.status_code is not None and not exc.retryable:
                log.warning("token exchange rejected by server '%s'", server.name)
                raise ExchangeRejectedError(
                    f"server '{server.name}' rejected the code exchange"
                ) from exc
            log.warning("token exchange failed for server '%s'", server.name)
            raise ExchangeError(f"token exchange failed for server '{server.name}'") from exc
        except AuthError as exc:
            log.warning("token exchange failed for server '%s'", server.name)
            raise ExchangeError(f"token exchange failed for server '{server.name}'") from exc

    async def _run_blocking(self, func: Callable[..., _T], /, *args: object) -> _T:
        """Run one blocking kstlib operation in the configured executor.

        Args:
            func: The blocking helper to run; it must not touch any
                orchestrator state.
            *args: Positional arguments passed through to the helper.

        Returns:
            The helper's result.
        """
        log.debug("dispatching %s to the executor", func.__name__)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, func, *args)

    async def _client_secret(self, server: ServerConfig) -> str:
        """Return the server's client secret, resolved once per process.

        The reference is read in the executor (it may hit the filesystem);
        the cache is checked and written on the event loop only. Two
        coroutines missing the cache concurrently both resolve the same
        deterministic value and the second write is an equivalent
        overwrite, so the race is benign: worst case the reference is read
        twice at startup.

        Args:
            server: The server whose secret reference is resolved.

        Returns:
            The resolved client secret.

        Raises:
            SecretResolutionError: If the reference cannot be resolved.
        """
        secret = self._secrets.get(server.name)
        if secret is None:
            secret = await self._run_blocking(resolve_client_secret, server)
            self._secrets[server.name] = secret
        return secret

    async def _build_provider(self, server: ServerConfig) -> OAuth2Provider:
        """Build the ephemeral kstlib provider for one operation.

        Construction is network-free (the kstlib HTTP client is lazy), so
        it happens on the event loop; only the provider's network calls run
        in the executor, which closes it afterwards. The configuration is
        built fresh for every operation: kstlib's OIDC constructor writes
        issuer-derived placeholder endpoints into the configuration object
        it receives, so a shared object would let a transiently failed
        discovery freeze those placeholders for the life of the process.
        Each provider gets a fresh in-memory token storage: kstlib saves
        the exchanged token into it, and it dies with the provider;
        tessera's encrypted store is the only persistence.

        Args:
            server: The server to build a provider for.

        Returns:
            A provider from the injected factory, or a real kstlib provider
            built from a fresh per-operation configuration.

        Raises:
            SecretResolutionError: If the client secret cannot be resolved.
        """
        secret = await self._client_secret(server)
        if self._provider_factory is not None:
            return self._provider_factory(server, secret)
        provider_config = self._build_provider_config(server, secret)
        if server.provider.uses_discovery:
            return OIDCProvider(server.name, provider_config, MemoryTokenStorage())
        return OAuth2Provider(server.name, provider_config, MemoryTokenStorage())

    def _build_provider_config(
        self, server: ServerConfig, client_secret: str
    ) -> AuthProviderConfig:
        """Translate a tessera server into a kstlib provider configuration.

        PKCE is disabled on the kstlib side on purpose: tessera generates
        the verifier and challenge itself, puts the challenge in its own
        authorization URL, and passes the verifier explicitly at exchange
        time, so the PKCE that reaches the wire is tessera's. The
        ``server_side`` profile declares the public redirect URI as the
        nominal configuration, so kstlib does not warn about it.

        Args:
            server: The server to translate.
            client_secret: The resolved client secret.

        Returns:
            The kstlib provider configuration.
        """
        if server.provider.uses_discovery:
            return AuthProviderConfig(
                client_id=server.client_id,
                client_secret=client_secret,
                issuer=server.provider.issuer,
                scopes=list(server.scopes),
                redirect_uri=self._config.service.redirect_uri,
                pkce=False,
                server_side=True,
            )
        return AuthProviderConfig(
            client_id=server.client_id,
            client_secret=client_secret,
            authorize_url=server.provider.authorization_endpoint,
            token_url=server.provider.token_endpoint,
            scopes=list(server.scopes),
            redirect_uri=self._config.service.redirect_uri,
            pkce=False,
            server_side=True,
        )
