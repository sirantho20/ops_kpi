"""Regression tests for chart month axis logic (no dashboard server import)."""

from __future__ import annotations

import unittest

import pandas as pd

from operations_kpi_data import CHART_MONTH_COUNT, _chart_month_axes


class ChartMonthAxesTests(unittest.TestCase):
    """_chart_month_axes must return the latest rolling chart window."""

    def test_returns_latest_twelve_months_when_data_spans_more(self) -> None:
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
            end=pd.Period("2026-03", "M"), periods=CHART_MONTH_COUNT, freq="M"
        )
        self.assertEqual(len(periods), CHART_MONTH_COUNT)
        self.assertEqual(list(periods), list(expected))
        self.assertEqual(labels[0], "Apr 2025")
        self.assertEqual(labels[-1], "Mar 2026")


if __name__ == "__main__":
    unittest.main()
