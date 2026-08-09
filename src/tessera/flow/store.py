"""Bounded, self-evicting in-memory store for in-flight OAuth flows.

Each ``/login`` registers a :class:`~tessera.flow.models.PendingFlow` keyed
by its anti-CSRF ``state``; the matching ``/callback`` consumes it exactly
once. The store keeps constant memory on a long-running service in two ways:
expired entries are swept on every add (amortized O(1)), and a hard
``max_size`` cap rejects further adds with
:class:`~tessera.flow.errors.PendingFlowStoreFull` rather than growing or
evicting a still-valid flow.

No state or code verifier is ever logged, even at DEBUG; only eviction and
capacity counts are.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable

from tessera.flow.errors import PendingFlowStoreFull
from tessera.flow.models import PendingFlow

log = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 600.0
_DEFAULT_MAX_SIZE = 10_000


class PendingFlowStore:
    """In-memory store of in-flight OAuth flows, bounded by TTL and size.

    Flows are held in insertion (creation) order, so the oldest entry is
    always at the front. Construct the store with a short TTL and a hard size
    cap; register flows with :meth:`add` and retrieve them once with
    :meth:`consume`.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        max_size: int = _DEFAULT_MAX_SIZE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Build a pending-flow store.

        Args:
            ttl_seconds: How long a registered flow stays valid before it is
                treated as expired.
            max_size: Hard cap on the number of unexpired flows held at once.
            clock: Callable returning the current Unix time in seconds.
                Injected so tests can control expiry without sleeping.
        """
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._clock = clock
        self._flows: OrderedDict[str, PendingFlow] = OrderedDict()

    def add(self, state: str, flow: PendingFlow) -> None:
        """Register a pending flow under its state.

        Expired entries are swept first; if the store is then still at
        capacity the flow is rejected so memory stays bounded and no valid
        in-flight flow is evicted.

        Args:
            state: The anti-CSRF state the flow is keyed by.
            flow: The pending flow to register.

        Raises:
            PendingFlowStoreFull: If the store already holds ``max_size``
                unexpired flows after sweeping.
        """
        now = self._clock()
        self._sweep_expired(now)
        if len(self._flows) >= self._max_size:
            log.warning(
                "[SECURITY] pending-flow store at capacity (max_size=%d), add rejected",
                self._max_size,
            )
            raise PendingFlowStoreFull(f"pending-flow store is at capacity ({self._max_size})")
        self._flows[state] = flow

    def consume(self, state: str) -> PendingFlow | None:
        """Remove and return the pending flow for a state, exactly once.

        A state is valid at most once: consuming it removes it, so a replay
        returns None. An expired flow also returns None.

        Args:
            state: The anti-CSRF state to consume.

        Returns:
            The pending flow, or None if the state is unknown or expired.
        """
        flow = self._flows.pop(state, None)
        if flow is None:
            return None
        if self._is_expired(flow, self._clock()):
            return None
        return flow

    def _sweep_expired(self, now: float) -> None:
        """Evict expired entries from the oldest end (amortized O(1))."""
        swept = 0
        while self._flows:
            oldest = next(iter(self._flows.values()))
            if not self._is_expired(oldest, now):
                break
            self._flows.popitem(last=False)
            swept += 1
        if swept:
            log.debug("swept %d expired pending flow(s)", swept)

    def _is_expired(self, flow: PendingFlow, now: float) -> bool:
        """Return whether a flow has reached or passed its TTL."""
        return now - flow.created_at >= self._ttl
