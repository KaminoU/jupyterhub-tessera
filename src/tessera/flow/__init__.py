"""tessera OAuth flow layer: state, PKCE, pending store, secrets, orchestration.

Public entry points: the crypto primitives (:func:`generate_state`,
:func:`generate_code_verifier`, :func:`compute_code_challenge`), the
:class:`PendingFlow`, :class:`AccessToken`, :class:`TokenStatus`, and
:class:`ServerInfo` value objects, the bounded :class:`PendingFlowStore` with its
:class:`PendingFlowStoreError` hierarchy, :func:`resolve_client_secret`
(the first real read of the client secret referenced by the
configuration), and :class:`FlowOrchestrator`, which ties them to
kstlib.auth for OIDC discovery, the back-channel code exchange, and the
rotation-aware access-token refresh. The flow errors extend
:class:`FlowError`. Everything here is callable without tornado; the HTTP
endpoints live in the service layer.
"""

from __future__ import annotations

from tessera.flow.crypto import (
    compute_code_challenge,
    generate_code_verifier,
    generate_state,
)
from tessera.flow.errors import (
    CallbackValidationError,
    ExchangeError,
    ExchangeRejectedError,
    FlowError,
    NoStoredTokenError,
    PendingFlowStoreError,
    PendingFlowStoreFull,
    RefreshRejectedError,
    SecretResolutionError,
    UnknownServerError,
)
from tessera.flow.models import AccessToken, PendingFlow, ServerInfo, TokenStatus
from tessera.flow.orchestrator import FlowOrchestrator, ProviderFactory
from tessera.flow.secrets import resolve_client_secret
from tessera.flow.store import PendingFlowStore

__all__ = [
    "AccessToken",
    "CallbackValidationError",
    "ExchangeError",
    "ExchangeRejectedError",
    "FlowError",
    "FlowOrchestrator",
    "NoStoredTokenError",
    "PendingFlow",
    "PendingFlowStore",
    "PendingFlowStoreError",
    "PendingFlowStoreFull",
    "ProviderFactory",
    "RefreshRejectedError",
    "SecretResolutionError",
    "ServerInfo",
    "TokenStatus",
    "UnknownServerError",
    "compute_code_challenge",
    "generate_code_verifier",
    "generate_state",
    "resolve_client_secret",
]
