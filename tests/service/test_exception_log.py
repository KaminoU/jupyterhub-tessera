"""HTTP tests for uncaught-exception logging: the query string never leaks.

An unmapped exception (a store write failure, an unexpected error) is not
caught by the browser handlers, so tornado logs it. Its default log line
carries the full request URI, which on the OAuth callback holds the
``code`` and ``state``; the service overrides ``log_exception`` to keep the
diagnostic (method, bare path, exception type, and the stack via
``exc_info``) while dropping every received value.
"""

from __future__ import annotations

import logging
from unittest import mock

from support import PREFIX, ServiceTestCase


class _RecordCollector(logging.Handler):
    """A logging handler collecting every record it is handed."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Store the record for later inspection."""
        self.records.append(record)


class TestUncaughtExceptionLog(ServiceTestCase):
    """An uncaught exception is logged without echoing the request query."""

    def test_callback_uncaught_exception_logs_no_code_or_state(self) -> None:
        """A store failure on /callback logs the bare path, never code/state."""
        collector = _RecordCollector()
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(collector)
        root.setLevel(logging.DEBUG)
        try:
            with mock.patch.object(
                self.orchestrator,
                "complete_callback",
                mock.AsyncMock(side_effect=RuntimeError("store write failed")),
            ):
                response = self.fetch(
                    f"{PREFIX}callback?code=leak-code-abc&state=leak-state-xyz",
                    headers=self.cookie_headers(),
                )
        finally:
            root.removeHandler(collector)
            root.setLevel(previous_level)
        assert response.code == 500
        rendered = "\n".join(record.getMessage() for record in collector.records)
        assert "uncaught exception" in rendered.lower()
        for needle in ("leak-code-abc", "leak-state-xyz", "code=leak", "state=leak"):
            assert needle not in rendered
