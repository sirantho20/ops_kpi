from __future__ import annotations

import unittest
from unittest import mock

import operations_kpi_data as data


class LoadOpsKpiTargetsLoggingTests(unittest.TestCase):
    def test_logs_warning_when_database_load_fails(self) -> None:
        with mock.patch(
            "operations_kpi_data.psycopg.connect",
            side_effect=RuntimeError("connection refused"),
        ):
            with self.assertLogs("operations_kpi.data", level="WARNING") as captured:
                result = data.load_ops_kpi_targets("postgresql://localhost/kpi")
        self.assertEqual(result, data.default_ops_kpi_targets())
        joined = "\n".join(captured.output)
        self.assertIn("default targets", joined)


if __name__ == "__main__":
    unittest.main()
