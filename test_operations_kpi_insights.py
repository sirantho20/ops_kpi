from __future__ import annotations

import csv
import io
import logging
import unittest

import pandas as pd

import operations_kpi_insights as insights
from operations_kpi_data import CSV_REQUIRED_COLUMNS


def _sample_df() -> pd.DataFrame:
    rows = []
    for date_text, incident in [("2025-06-01", 2), ("2025-06-02", 1), ("2025-07-01", 0)]:
        date_value = pd.Timestamp(date_text)
        row = {col: 0 for col in CSV_REQUIRED_COLUMNS}
        row.update(
            {
                "Date": date_value,
                "Region": "NCR",
                "Zoo": "Z1",
                "PLA ID": "PLA1",
                "PTCI Number": "PTCI1",
                "Incident_count": incident,
                "Outage_mins": 0,
                "Accepted Outage Minutes": 0,
                "Availability": 1.0,
                "Uptime_per_tenant": 1.0,
                "Total Available Minutes": 1440,
                "SIC Count": 0,
                "Site Visit Count": 0,
                "CM Count": 0,
            }
        )
        rows.append(row)
    df = pd.DataFrame(rows)
    df["ptci_site_id"] = "PTCI1"
    df["availability_weight"] = df["Total Available Minutes"]
    df["availability_ratio"] = df["Availability"]
    df["availability_fallback_ratio"] = df["Uptime_per_tenant"]
    df["month_period"] = df["Date"].dt.to_period("M")
    return df


class ResolveCellPeriodDfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.df = _sample_df()
        self.periods = {
            "Jun_25": (
                (self.df["Date"] == pd.Timestamp("2025-06-01"))
                | (self.df["Date"] == pd.Timestamp("2025-06-02"))
            ),
            "Jul_25": self.df["Date"] == pd.Timestamp("2025-07-01"),
        }

    def test_resolve_returns_rows_for_region_and_period(self) -> None:
        period_df = insights.resolve_cell_period_df(
            self.df,
            self.periods,
            "region",
            "NCR",
            None,
            "events",
            "Jun_25",
        )
        self.assertEqual(len(period_df), 2)
        self.assertTrue((period_df["Region"] == "NCR").all())

    def test_resolve_returns_empty_for_target(self) -> None:
        period_df = insights.resolve_cell_period_df(
            self.df,
            self.periods,
            "region",
            "NCR",
            None,
            "events",
            "TARGET",
        )
        self.assertTrue(period_df.empty)


class BuildCellInsightCsvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.df = _sample_df()
        self.periods = {
            "Jun_25": (
                (self.df["Date"] == pd.Timestamp("2025-06-01"))
                | (self.df["Date"] == pd.Timestamp("2025-06-02"))
            ),
        }

    def test_build_csv_includes_headers_and_data_rows(self) -> None:
        body, filename = insights.build_cell_insight_csv(
            self.df,
            self.periods,
            "region",
            "NCR",
            None,
            "events",
            "Jun_25",
        )
        self.assertTrue(filename.endswith(".csv"))
        self.assertIn("ops_kpi_events_NCR_Jun_25.csv", filename)
        text = body.decode("utf-8")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        self.assertIn("Date", rows[0])
        self.assertIn("Incident_count", rows[0])
        self.assertEqual(len(rows), 3)

    def test_target_raises_value_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            insights.build_cell_insight_csv(
                self.df,
                self.periods,
                "region",
                "NCR",
                None,
                "events",
                "TARGET",
            )
        self.assertIn("TARGET", str(ctx.exception))

    def test_invalid_period_logs_warning(self) -> None:
        with self.assertLogs("operations_kpi.insights", level="WARNING") as captured:
            with self.assertRaises(ValueError):
                insights._validate_metric_period(
                    "events",
                    "NotAPeriod",
                    self.periods,
                    {},
                )
        self.assertTrue(
            any("Invalid period" in line for line in captured.output)
        )


if __name__ == "__main__":
    unittest.main()
