"""Tests for the bounded pending-flow store (PendingFlowStore).

Memory-leak coverage comes first, because the constant-memory guarantee is
the point of this store: expired entries are reclaimed on every add, and a
hard size cap rejects further adds instead of growing. The clock is injected
so expiry is deterministic without any real sleep.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from tessera.flow import (
    PendingFlow,
    PendingFlowStore,
    PendingFlowStoreError,
    PendingFlowStoreFull,
)


class _Clock:
    """A controllable clock returning a fixed value until moved."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _flow(
    username: str = "alice",
    provider: str = "keycloak",
    verifier: str = "verifier-value",
    created_at: float = 0.0,
) -> PendingFlow:
    """Return a PendingFlow with sensible defaults for store tests."""
    return PendingFlow(
        username=username,
        provider=provider,
        code_verifier=verifier,
        created_at=created_at,
    )


class TestMemoryReclamation:
    """The store keeps constant memory regardless of abandoned flows."""

    def test_expired_entry_reclaimed_on_next_add(self) -> None:
        """An expired flow is swept when a later add runs, before its consume."""
        clock = _Clock(0.0)
        store = PendingFlowStore(ttl_seconds=600.0, max_size=10_000, clock=clock)
        store.add("state-a", _flow(created_at=0.0))
        clock.now = 601.0  # state-a is now past its TTL
        store.add("state-b", _flow(created_at=601.0))
        # state-a was swept during the add of state-b, not merely on consume.
        assert store.consume("state-a") is None
        assert store.consume("state-b") is not None

    def test_cap_rejects_when_full(self) -> None:
        """A full store rejects further adds; existing flows are untouched."""
        clock = _Clock(0.0)
        store = PendingFlowStore(ttl_seconds=600.0, max_size=3, clock=clock)
        for i in range(3):
            store.add(f"state-{i}", _flow(created_at=0.0))
        with pytest.raises(PendingFlowStoreFull):
            store.add("state-overflow", _flow(created_at=0.0))
        # The three originals were not evicted to make room.
        for i in range(3):
            assert store.consume(f"state-{i}") is not None

    def test_full_store_accepts_again_after_ttl_frees_space(self) -> None:
        """Once entries expire, a previously full store accepts new flows."""
        clock = _Clock(0.0)
        store = PendingFlowStore(ttl_seconds=600.0, max_size=2, clock=clock)
        store.add("s0", _flow(created_at=0.0))
        store.add("s1", _flow(created_at=0.0))
        with pytest.raises(PendingFlowStoreFull):
            store.add("s2", _flow(created_at=0.0))
        clock.now = 601.0  # s0 and s1 expire
        store.add("s2", _flow(created_at=601.0))  # the sweep frees room
        assert store.consume("s2") is not None
        assert store.consume("s0") is None
        assert store.consume("s1") is None

    def test_flood_of_abandoned_flows_stays_bounded(self) -> None:
        """A flood of never-completed logins never grows past max_size."""
        store = PendingFlowStore(ttl_seconds=600.0, max_size=5, clock=_Clock(0.0))
        rejected = 0
        for i in range(100):
            try:
                store.add(f"s{i}", _flow(created_at=0.0))
            except PendingFlowStoreFull:
                rejected += 1
        # Only max_size were accepted; the remaining 95 were rejected.
        assert rejected == 95


class TestConsumeSemantics:
    """consume is one-shot and honors expiry."""

    def test_consume_returns_the_stored_flow(self) -> None:
        """consume returns the exact flow that was added."""
        store = PendingFlowStore(clock=_Clock(0.0))
        store.add("s", _flow(username="bob", provider="okta", verifier="the-verifier"))
        flow = store.consume("s")
        assert flow is not None
        assert flow.username == "bob"
        assert flow.provider == "okta"
        assert flow.code_verifier == "the-verifier"

    def test_consume_is_one_shot(self) -> None:
        """A consumed state is never valid again (replay returns None)."""
        store = PendingFlowStore(clock=_Clock(0.0))
        store.add("s", _flow())
        assert store.consume("s") is not None
        assert store.consume("s") is None

    def test_consume_unknown_returns_none(self) -> None:
        """Consuming a state that was never added returns None."""
        store = PendingFlowStore(clock=_Clock(0.0))
        assert store.consume("never-added") is None

    def test_consume_expired_returns_none(self) -> None:
        """Consuming a flow past its TTL returns None."""
        clock = _Clock(0.0)
        store = PendingFlowStore(ttl_seconds=600.0, clock=clock)
        store.add("s", _flow(created_at=0.0))
        clock.now = 601.0
        assert store.consume("s") is None

    def test_flow_is_expired_at_exactly_ttl(self) -> None:
        """A flow reaching its TTL exactly is expired (boundary, inclusive)."""
        clock = _Clock(0.0)
        store = PendingFlowStore(ttl_seconds=600.0, clock=clock)
        store.add("s", _flow(created_at=0.0))
        clock.now = 600.0
        assert store.consume("s") is None

    def test_flow_is_valid_just_before_ttl(self) -> None:
        """A flow just short of its TTL is still valid."""
        clock = _Clock(0.0)
        store = PendingFlowStore(ttl_seconds=600.0, clock=clock)
        store.add("s", _flow(created_at=0.0))
        clock.now = 599.9
        assert store.consume("s") is not None


class TestErrors:
    """The store error hierarchy is tessera-owned."""

    def test_store_error_is_exception(self) -> None:
        """PendingFlowStoreError is a plain Exception subclass."""
        assert issubclass(PendingFlowStoreError, Exception)

    def test_full_is_store_error(self) -> None:
        """PendingFlowStoreFull is a PendingFlowStoreError."""
        assert issubclass(PendingFlowStoreFull, PendingFlowStoreError)


class TestLoggingHygiene:
    """The store logs eviction counts only, never a state or verifier."""

    def test_no_state_or_verifier_in_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """No log entry (even DEBUG) contains a state or code verifier."""
        clock = _Clock(0.0)
        store = PendingFlowStore(ttl_seconds=600.0, max_size=1, clock=clock)
        secret_state = "SECRET-STATE-abc"
        secret_verifier = "SECRET-VERIFIER-xyz"
        with caplog.at_level(logging.DEBUG):
            store.add(secret_state, _flow(verifier=secret_verifier, created_at=0.0))
            clock.now = 601.0
            store.add("state-2", _flow(created_at=601.0))  # sweeps secret_state
            with pytest.raises(PendingFlowStoreFull):
                store.add("state-3", _flow(created_at=601.0))  # store full
        assert secret_state not in caplog.text
        assert secret_verifier not in caplog.text


class TestConcurrentConsume:
    """Functional concurrency: the one-shot contract holds under gather."""

    async def test_double_consume_has_exactly_one_winner(self) -> None:
        """Two concurrent consumers of one state get exactly one flow between them."""
        clock = _Clock(0.0)
        store = PendingFlowStore(ttl_seconds=600.0, max_size=10, clock=clock)
        store.add("state-1", _flow(created_at=0.0))

        async def consume() -> PendingFlow | None:
            return store.consume("state-1")

        first, second = await asyncio.gather(consume(), consume())
        assert [first, second].count(None) == 1
        winner = first if first is not None else second
        assert winner is not None
        assert winner.username == "alice"
