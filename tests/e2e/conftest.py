"""Fixtures for the end-to-end bench suite: skip-guard and Hub sessions."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from bench import BENCH_HELP, HubSession, bench_is_up


@pytest.fixture(scope="session")
def bench() -> None:
    """Skip the whole suite with an actionable message when the bench is down."""
    if not bench_is_up():
        pytest.skip(BENCH_HELP)


@pytest.fixture
def hub_session(bench: None) -> Iterator[HubSession]:
    """An authenticated Hub session for a fresh ephemeral user."""
    session = HubSession()
    session.login_hub()
    yield session
    session.close()


@pytest.fixture
def second_session(bench: None) -> Iterator[HubSession]:
    """A second, independent authenticated Hub session (isolation tests)."""
    session = HubSession()
    session.login_hub()
    yield session
    session.close()
