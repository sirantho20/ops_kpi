"""Regression tests for chart month axis logic (no dashboard server import)."""

from __future__ import annotations

import unittest

import pandas as pd

from operations_kpi_data import CHART_MONTH_START, _chart_month_axes


class ChartMonthAxesTests(unittest.TestCase):
    """_chart_month_axes must start at CHART_MONTH_START, not df['month_period'].min()."""

    def test_starts_january_2025_when_data_starts_august_2025(self) -> None:
        df = pd.DataFrame(
            {
                "month_period": [
                    pd.Period("2025-08", "M"),
                    pd.Period("2026-03", "M"),
                ],
            }
        )
        periods, labels = _chart_month_axes(df)
        expected = pd.period_range(
            start=CHART_MONTH_START, end=pd.Period("2026-03", "M"), freq="M"
        )
        self.assertEqual(len(periods), len(expected))
        self.assertEqual(list(periods), list(expected))
        self.assertEqual(labels[0], "Jan 2025")
        self.assertEqual(labels[-1], "Mar 2026")


if __name__ == "__main__":
    unittest.main()
