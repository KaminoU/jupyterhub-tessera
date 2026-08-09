"""HTTP tests for the health root: the unauthenticated liveness probe.

The bare service prefix answers a minimal, static, secret-free body so
the Hub/proxy liveness poll stops flooding the logs with 404s; its
access-log line is demoted to DEBUG while the real endpoints stay at
INFO, so the poll noise disappears without hiding genuine traffic.
"""

from __future__ import annotations

import json
import logging

from support import PREFIX, ServiceTestCase


class _RecordCollector(logging.Handler):
    """A logging handler collecting every record it is handed."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Store the record for later inspection."""
        self.records.append(record)


class TestHealthContract(ServiceTestCase):
    """The health root is public, minimal, and free of any sensitive value."""

    def test_health_is_public_and_minimal(self) -> None:
        """The bare prefix answers 200 with the exact minimal body, no auth."""
        response = self.fetch(PREFIX)
        assert response.code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert json.loads(response.body) == {"service": "tessera", "status": "ok"}

    def test_health_body_carries_nothing_sensitive(self) -> None:
        """The body never carries a token, secret, credential, or config value."""
        body = self.fetch(PREFIX).body.decode("utf-8")
        for needle in ("token", "secret", "client", "key", "password", "://"):
            assert needle not in body


class TestHealthAccessLog(ServiceTestCase):
    """The health access-log is demoted to DEBUG; real endpoints stay INFO."""

    def test_health_logs_at_debug_but_real_endpoints_stay_info(self) -> None:
        """Only the health hit is DEBUG; the status hit is INFO."""
        logger = logging.getLogger("tessera.service.app")
        collector = _RecordCollector()
        previous_level = logger.level
        logger.addHandler(collector)
        logger.setLevel(logging.DEBUG)
        try:
            self.fetch(PREFIX)
            self.fetch(
                self.service_url("status", server="manual-idp"),
                headers=self.token_headers(),
            )
        finally:
            logger.removeHandler(collector)
            logger.setLevel(previous_level)

        def level_for(path: str) -> int | None:
            for record in collector.records:
                parts = record.getMessage().split()
                if len(parts) >= 3 and parts[2] == path:
                    return record.levelno
            return None

        assert level_for(PREFIX) == logging.DEBUG
        assert level_for(f"{PREFIX}status") == logging.INFO

    def test_health_error_status_is_not_demoted(self) -> None:
        """A 4xx on the health root stays INFO: a scan burst must be visible."""
        logger = logging.getLogger("tessera.service.app")
        collector = _RecordCollector()
        previous_level = logger.level
        logger.addHandler(collector)
        logger.setLevel(logging.DEBUG)
        try:
            response = self.fetch(PREFIX, method="POST", body=b"")
        finally:
            logger.removeHandler(collector)
            logger.setLevel(previous_level)
        assert response.code >= 400

        def level_for(path: str) -> int | None:
            for record in collector.records:
                parts = record.getMessage().split()
                if len(parts) >= 3 and parts[2] == path:
                    return record.levelno
            return None

        assert level_for(PREFIX) == logging.INFO
