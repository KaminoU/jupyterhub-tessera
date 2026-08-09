"""Stress bench against the live local bench (tier 3+, opt-in, real sleeps).

Precondition: the bench is up (``cd infra && docker compose up -d &&
jupyterhub -f infra/jupyterhub_config.py``) with the tessera service
reachable from WSL2, otherwise the suite is skipped. Run it with
``pytest -m stress``; the default run excludes both the ``stress`` and the
``e2e`` markers.

This turns the manual stress/RSS protocol into a replayable module. It
drives four load shapes at the single live service, in one process, while
a background sampler records that service's resident set size (RSS) from
``/proc/<pid>/status`` inside WSL2:

  a. a sequential token-refresh regime on the rotating realm, the harshest
     sustained path through the encrypted store (the refresh token is
     rotated on every use),
  b. a concurrent burst on the same user and server, exercising the
     service's single-flight refresh coalescing under real threads,
  c. a flood of abandoned logins, piling up pending flows well under the
     store's hard cap,
  d. a quiet tail, sampled after the load stops, judged by a lenient RSS
     canary that fires only on a genuine leak.

Real sleeps and real wall-clock latencies are the point, the same
documented exception to the mocked-clock rule as the e2e tier. Expect
roughly 10 to 15 minutes.

Tokens never appear in assertions, prints, or the CSV: only latencies,
status codes, and RSS in kiB are reported. The CSV lives under a pytest
tmp dir and is never committed.
"""

from __future__ import annotations

import csv
import statistics
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from bench import HubSession

pytestmark = pytest.mark.stress

_RSS_SAMPLE_INTERVAL_S = 10.0
_POLL_INTERVAL_S = 2.0
_TAIL_SLEEP_S = 120.0
_SEQUENTIAL_CALLS = 300
_BURST_WORKERS = 20
_BURST_CALLS_PER_WORKER = 10
_FLOOD_CALLS = 1500
_CANARY_MAX_RATIO = 1.5
_CANARY_MIN_SAMPLES = 8
_WSL_TIMEOUT_S = 5.0
_ROTATING = "rotating"
_FLOOD_SERVER = "stable-discovery"
_IDP_USER = "alice"


def _resolve_service_pid() -> int | None:
    """Return the tessera service PID inside WSL2, or None when not found.

    Resolved fresh on each sample so the sampler follows a service respawn
    instead of pinning a stale PID.

    Returns:
        The first matching PID, or None if WSL2 is unreachable or the
        service is not running.
    """
    try:
        result = subprocess.run(
            ["wsl.exe", "pgrep", "-f", "tessera.service"],
            capture_output=True,
            text=True,
            timeout=_WSL_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    pids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not pids:
        return None
    try:
        return int(pids[0])
    except ValueError:
        return None


def _read_vmrss_kib(pid: int) -> int | None:
    """Return the process VmRSS in kiB from WSL2 ``/proc``, or None on failure.

    Args:
        pid: The Linux PID to inspect inside WSL2.

    Returns:
        The VmRSS value in kiB, or None if the status could not be read
        (a transient respawn window, typically).
    """
    try:
        result = subprocess.run(
            ["wsl.exe", "cat", f"/proc/{pid}/status"],
            capture_output=True,
            text=True,
            timeout=_WSL_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1])
                except ValueError:
                    return None
    return None


def _quartile_medians(kibs: list[int]) -> tuple[float, float]:
    """Return the median RSS of the first and last quarter of a sample list.

    Args:
        kibs: The ordered RSS samples in kiB.

    Returns:
        A ``(first_quarter_median, last_quarter_median)`` pair.
    """
    quarter = max(1, len(kibs) // 4)
    return statistics.median(kibs[:quarter]), statistics.median(kibs[-quarter:])


def _print_latencies(label: str, latencies: list[float]) -> None:
    """Print p50/p95/max of a latency sample, in milliseconds.

    Args:
        label: A short tag identifying the salvo.
        latencies: The per-call wall-clock latencies in seconds.
    """
    p50 = statistics.median(latencies) * 1000
    p95 = statistics.quantiles(latencies, n=20)[-1] * 1000
    peak = max(latencies) * 1000
    print(f"\n[{label}] n={len(latencies)} p50={p50:.1f} p95={p95:.1f} max={peak:.1f} ms")


class RssSampler:
    """Background sampler of the live service RSS, writing a timestamped CSV.

    The PID is resolved fresh on each tick, so a service respawn is followed
    rather than pinned. Ticks that cannot be read are skipped, never fatal.
    The CSV holds ``timestamp,kib`` rows, where the timestamp is seconds
    elapsed since the sampler started.
    """

    def __init__(self, csv_path: Path) -> None:
        """Prepare a sampler writing to csv_path; call :meth:`start` to begin.

        Args:
            csv_path: Destination CSV, created when the thread starts.
        """
        self.csv_path = csv_path
        self._samples: list[tuple[float, int]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = 0.0

    def start(self) -> None:
        """Launch the background sampling thread."""
        self._started_at = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="rss-sampler", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """Sample VmRSS every interval until stopped, into memory and the CSV."""
        with self.csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["timestamp", "kib"])
            while not self._stop.is_set():
                pid = _resolve_service_pid()
                kib = _read_vmrss_kib(pid) if pid is not None else None
                if kib is not None:
                    elapsed = round(time.monotonic() - self._started_at, 2)
                    with self._lock:
                        self._samples.append((elapsed, kib))
                    writer.writerow([elapsed, kib])
                    handle.flush()
                self._stop.wait(_RSS_SAMPLE_INTERVAL_S)

    def stop(self) -> None:
        """Signal the thread to stop and join it within a bounded delay."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=_WSL_TIMEOUT_S + _RSS_SAMPLE_INTERVAL_S)

    def snapshot(self) -> list[tuple[float, int]]:
        """Return a copy of the samples collected so far.

        Returns:
            A list of ``(elapsed_seconds, kib)`` pairs.
        """
        with self._lock:
            return list(self._samples)

    def print_summary(self) -> None:
        """Print the CSV path and a coarse min/max/quartile RSS summary."""
        samples = self.snapshot()
        print(f"\n[rss] csv: {self.csv_path}")
        if not samples:
            print("[rss] no samples collected")
            return
        kibs = [kib for _, kib in samples]
        q1, q4 = _quartile_medians(kibs)
        print(
            f"[rss] samples={len(kibs)} min={min(kibs)} max={max(kibs)} "
            f"median_Q1={q1:.0f} median_Q4={q4:.0f} kiB"
        )


@pytest.fixture(scope="module", autouse=True)
def rss_sampler(bench: None, tmp_path_factory: pytest.TempPathFactory) -> Iterator[RssSampler]:
    """Sample the service RSS for the whole module, or skip if unsampleable.

    Yields:
        The running sampler, so the tail canary can read its samples.
    """
    pid = _resolve_service_pid()
    if pid is None or _read_vmrss_kib(pid) is None:
        pytest.skip("service RSS not sampleable from WSL2 (wsl.exe pgrep/proc unavailable)")
    sampler = RssSampler(tmp_path_factory.mktemp("stress") / "rss.csv")
    sampler.start()
    yield sampler
    sampler.stop()
    sampler.print_summary()


@pytest.fixture(scope="module", autouse=True)
def poll_thread(bench: None) -> Iterator[None]:
    """Poll the status endpoint every 2 s in the background (the panel load)."""
    session = HubSession()
    session.login_hub()
    stop = threading.Event()
    counters = {"ok": 0, "err": 0}

    def _poll() -> None:
        while not stop.is_set():
            try:
                session.status_json(_ROTATING)
                counters["ok"] += 1
            except Exception:  # background load must survive a transient blip
                counters["err"] += 1
            stop.wait(_POLL_INTERVAL_S)

    thread = threading.Thread(target=_poll, name="panel-poll", daemon=True)
    thread.start()
    yield
    stop.set()
    thread.join(timeout=_POLL_INTERVAL_S + _WSL_TIMEOUT_S)
    session.close()
    print(f"\n[poll] status calls ok={counters['ok']} err={counters['err']}")


def test_a_sequential_refresh_regime(hub_session: HubSession) -> None:
    """300 sequential token calls on the rotating realm all succeed.

    The rotating realm rotates the refresh token on every use, so this is
    the harshest sustained path through the encrypted store.
    """
    hub_session.complete_flow(_ROTATING, _IDP_USER)
    api_token = hub_session.api_token()
    latencies: list[float] = []
    for _ in range(_SEQUENTIAL_CALLS):
        start = time.perf_counter()
        response = hub_session.token_endpoint(_ROTATING, api_token)
        latencies.append(time.perf_counter() - start)
        assert response.status_code == 200
        assert response.json()["access_token"]
    _print_latencies("seq", latencies)


def test_b_concurrent_burst_coalescing(hub_session: HubSession) -> None:
    """A 20-thread burst on one user and server stays all-200.

    Concurrent calls to the same (user, server) exercise the service's
    single-flight refresh coalescing under real threads.
    """
    hub_session.complete_flow(_ROTATING, _IDP_USER)
    api_token = hub_session.api_token()

    def _call() -> tuple[int, float]:
        start = time.perf_counter()
        response = hub_session.token_endpoint(_ROTATING, api_token)
        return response.status_code, time.perf_counter() - start

    total = _BURST_WORKERS * _BURST_CALLS_PER_WORKER
    with ThreadPoolExecutor(max_workers=_BURST_WORKERS) as pool:
        futures = [pool.submit(_call) for _ in range(total)]
        results = [future.result() for future in futures]
    statuses = [status for status, _ in results]
    latencies = [latency for _, latency in results]
    assert statuses == [200] * total
    _print_latencies("burst", latencies)


def test_c_abandoned_login_flood(hub_session: HubSession) -> None:
    """1500 never-completed logins pile up pending flows under the cap.

    Each call registers a pending flow server-side (well under the store's
    10 000 cap) and is never completed; the RSS canary must stay flat.
    """
    for _ in range(_FLOOD_CALLS):
        state = hub_session.authorization_state(_FLOOD_SERVER)
        assert state
    print(f"\n[flood] started {_FLOOD_CALLS} pending flows on {_FLOOD_SERVER!r}")


def test_d_rss_tail_canary(rss_sampler: RssSampler) -> None:
    """The idle RSS tail must not exceed 1.5x the loaded first quarter.

    A deliberately coarse guard: it fires only on a genuine leak (the last
    quarter well above the first). The fine slope verdict is read off the
    CSV offline, not asserted here.
    """
    time.sleep(_TAIL_SLEEP_S)
    samples = rss_sampler.snapshot()
    assert len(samples) >= _CANARY_MIN_SAMPLES, (
        f"RSS sampler produced too few samples ({len(samples)}) to judge the tail"
    )
    kibs = [kib for _, kib in samples]
    first_median, last_median = _quartile_medians(kibs)
    ratio = last_median / first_median
    print(f"\n[canary] Q1={first_median:.0f} Q4={last_median:.0f} kiB ratio={ratio:.2f}")
    assert last_median <= first_median * _CANARY_MAX_RATIO
