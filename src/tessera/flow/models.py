"""Value objects for the OAuth flow layer.

:class:`PendingFlow` tracks an in-flight authorization-code flow awaiting
its callback; :class:`AccessToken` is a freshly refreshed access token with
its metadata; :class:`TokenStatus` is the cheap stored-token status polled
by the frontend panel; :class:`ServerInfo` is the per-server button
descriptor the panel loads once at startup. Secret-bearing fields are
excluded from ``repr`` so they cannot leak into logs or tracebacks (token
hygiene).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PendingFlow:
    """An in-flight OAuth flow awaiting its callback, keyed by state.

    Holds the per-flow PKCE code verifier so the callback can complete the
    token exchange. The ``code_verifier`` is excluded from ``repr`` so it
    cannot leak into logs or tracebacks (token hygiene). ``created_at`` is
    Unix seconds from the store's clock and drives TTL expiry.
    """

    username: str
    provider: str
    code_verifier: str = field(repr=False)
    created_at: float


@dataclass(frozen=True, slots=True)
class AccessToken:
    """A freshly refreshed OAuth access token with its metadata.

    Returned by
    :meth:`~tessera.flow.orchestrator.FlowOrchestrator.get_access_token`.
    The ``value`` field is excluded from ``repr`` so the token cannot leak
    into logs or tracebacks (token hygiene). This object is never persisted:
    only the refresh token is stored, encrypted, by the token store.
    """

    value: str = field(repr=False)
    expires_at: float | None
    scope: str | None


@dataclass(frozen=True, slots=True)
class TokenStatus:
    """The stored-token status for a (user, server) pair, cheap to poll.

    ``has_token`` is the button's truth: a stored refresh token exists and
    is presumed valid. Every other field is detail for the panel, read
    from the same single store row. ``expires_at`` is the access-token
    expiry observed at the last store write, indicative only. The
    ``refresh_kind`` is derived from the stored scope: ``"offline"`` when
    ``offline_access`` was granted, ``"session"`` when a scope was granted
    without it, ``"unknown"`` when no scope is stored (including when no
    token is stored at all); it is always one of those three literals,
    never None, so a consumer can switch on it safely.
    ``refresh_expires_at`` is the dated expiry of the refresh token itself
    when the provider communicated one, None otherwise (never
    extrapolated). Actual invalidity is discovered, and the record
    deleted, at the next
    :meth:`~tessera.flow.orchestrator.FlowOrchestrator.get_access_token`.
    """

    has_token: bool
    expires_at: float | None
    created_at: float | None
    updated_at: float | None
    scope: str | None
    refresh_kind: str
    refresh_expires_at: float | None


@dataclass(frozen=True, slots=True)
class ServerInfo:
    """The per-server button descriptor served to the frontend panel.

    A pure projection of the validated configuration (name, display
    label, and state colors): it carries nothing secret and nothing
    provider-facing, so the panel can render the button list without
    ever seeing a client id, an endpoint, or a token.
    """

    name: str
    label: str
    color_valid: str
    color_invalid: str
