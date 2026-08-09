"""Value object for a persisted OAuth refresh token record."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TokenRecord:
    """A stored OAuth refresh token and its metadata, keyed by (username, provider).

    Only the long-lived refresh token is persisted, never the access token.
    The ``refresh_token`` field is excluded from ``repr`` so it cannot leak
    into logs or tracebacks (token hygiene). Timestamps are Unix seconds as
    produced by the store's clock. ``refresh_expires_at`` is the expiry of
    the refresh token itself, when the provider dated it: None means the
    provider communicated no expiry (an offline token, or a provider that
    stays silent), never an extrapolated value.
    """

    username: str
    provider: str
    refresh_token: str = field(repr=False)
    expires_at: float | None
    scope: str | None
    refresh_expires_at: float | None
    created_at: float
    updated_at: float
