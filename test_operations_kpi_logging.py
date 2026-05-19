from __future__ import annotations

import logging
import unittest

from operations_kpi_logging import (
    configure_logging,
    log_db_url_safe,
    log_timing,
    resolve_log_level,
)


class OperationsKpiLoggingTests(unittest.TestCase):
    def test_resolve_log_level_defaults_to_info(self) -> None:
        self.assertEqual(resolve_log_level("DEBUG"), logging.DEBUG)
        self.assertEqual(resolve_log_level("bogus"), logging.INFO)

    def test_configure_logging_sets_level(self) -> None:
        configure_logging("WARNING")
        root = logging.getLogger()
        self.assertEqual(root.level, logging.WARNING)

    def test_log_db_url_safe_strips_credentials(self) -> None:
        safe = log_db_url_safe("postgresql://user:secret@db.example.com:5432/kpi")
        self.assertNotIn("secret", safe)
        self.assertIn("db.example.com", safe)
        self.assertIn("kpi", safe)

    def test_log_db_url_safe_unset(self) -> None:
        self.assertEqual(log_db_url_safe(None), "<unset>")

    def test_log_timing_emits_start_and_finish(self) -> None:
        configure_logging("DEBUG")
        logger = logging.getLogger("test.operations_kpi_logging.timing")
        with self.assertLogs(logger, level="DEBUG") as captured:
            with log_timing(logger, "unit_test", rows=3):
                pass
        joined = "\n".join(captured.output)
        self.assertIn("unit_test started", joined)
        self.assertIn("unit_test finished", joined)
        self.assertIn("rows=3", joined)


if __name__ == "__main__":
    unittest.main()
