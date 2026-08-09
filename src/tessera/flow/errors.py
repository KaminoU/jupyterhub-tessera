"""Typed errors for the tessera OAuth flow.

Two independent hierarchies live here: :class:`PendingFlowStoreError`
covers the bounded pending-flow store (capacity), while :class:`FlowError`
covers the flow orchestration itself. No error message ever includes a
secret, a token, a state value, or an authorization code (token hygiene).
"""

from __future__ import annotations


class FlowError(Exception):
    """Base class for OAuth flow orchestration errors."""


class SecretResolutionError(FlowError):
    """The configured client secret reference could not be resolved.

    Raised by :func:`~tessera.flow.secrets.resolve_client_secret` when the
    reference cannot produce a usable value: an environment variable that
    is unset or blank, or a secret file that is missing, unreadable, empty,
    holds a null byte, or (on POSIX) is accessible by group or others. The
    secret value itself is never included in the message.
    """


class UnknownServerError(FlowError):
    """A flow was requested for a server name that is not declared.

    The message carries at most a truncated form of the requested name; no
    secret material is involved.
    """


class CallbackValidationError(FlowError):
    """An OAuth callback failed validation and was rejected.

    Covers a state that is missing, oversized, unknown, expired, replayed,
    or bound to another user, and an authorization code that is missing or
    oversized. The [SECURITY] log entry categorizes the reason; neither the
    message nor the log ever carries the state, the code, or any token
    material.
    """


class ExchangeError(FlowError):
    """The provider interaction failed during a flow.

    Wraps kstlib's discovery errors (raised as early as ``begin_login`` in
    discovery mode), failed exchange interactions (network errors, invalid
    responses), and transient refresh failures behind tessera's error
    surface, and also covers a token response that carries no refresh
    token. A provider that actually answered the code exchange with an
    OAuth error raises :class:`ExchangeRejectedError` instead, and only a
    definitive refresh rejection (:class:`RefreshRejectedError`)
    invalidates a stored token. The message names the server, never the
    authorization code, a token, or the PKCE verifier; the original error
    stays available as ``__cause__``.
    """


class ExchangeRejectedError(FlowError):
    """The provider answered the code exchange with a definitive error.

    Symmetric to :class:`RefreshRejectedError`, but for the initial
    authorization-code exchange: the provider was reached and responded
    with an OAuth error (a policy rejection or a client misconfiguration,
    for example), so retrying without fixing the cause will fail again.
    The message names the server; the provider's error code and
    description are never included (token hygiene).
    """


class NoStoredTokenError(FlowError):
    """An access token was requested for a pair with no stored refresh token.

    The user never completed a login for this server (or the stored token
    was invalidated after a rejected refresh). The message is actionable:
    it tells the user to sign in with the tessera button; it never carries
    any secret material.
    """


class RefreshRejectedError(FlowError):
    """The provider definitively rejected the stored refresh token.

    Covers an expired, revoked, or already-rotated (replayed) refresh
    token: indistinguishable cases on the client side, all reported by the
    provider as an ``invalid_grant``-class rejection. The stored record has
    been deleted by the time this error is raised, so the status turns red
    and the user must sign in again; a ``[SECURITY]`` log line categorizes
    the event without carrying any token value.
    """


class PendingFlowStoreError(Exception):
    """Base class for pending-flow store errors."""


class PendingFlowStoreFull(PendingFlowStoreError):
    """The pending-flow store is at capacity.

    Raised by :meth:`~tessera.flow.store.PendingFlowStore.add` when the store
    already holds ``max_size`` unexpired flows. The cap is a hard bound, so a
    flood of never-completed logins keeps memory constant instead of growing;
    a valid in-flight flow is never evicted before its own TTL.
    """
