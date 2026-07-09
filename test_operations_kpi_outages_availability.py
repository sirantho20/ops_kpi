"""Tests: Outages, MTTR, and availability chart actuals from ops_kpi_availability-only SQL."""

from __future__ import annotations

import unittest

import pandas as pd

from operations_kpi_data import (
    CHART_MONTH_COUNT,
    CHART_MONTH_START,
    _align_monthly_availability_to_list,
    _align_monthly_incident_sums_to_list,
    _align_monthly_mttr_to_list,
    _chart_month_axes_for_payload,
    fetch_monthly_charts_from_availability_only,
    fetch_ops_kpi_availability_cubes,
)


class AlignMonthlyIncidentSumsTests(unittest.TestCase):
    def test_fills_zero_for_missing_months(self) -> None:
        periods = pd.period_range("2025-01", "2025-03", freq="M")
        m = {pd.Timestamp("2025-02-01").date(): 5.0}
        out = _align_monthly_incident_sums_to_list(periods, m)
        self.assertEqual(out, [0, 5, 0])


class AlignMonthlyMttrTests(unittest.TestCase):
    def test_fills_none_for_missing_months(self) -> None:
        periods = pd.period_range("2025-01", "2025-03", freq="M")
        m = {pd.Timestamp("2025-02-01").date(): 42.666}
        out = _align_monthly_mttr_to_list(periods, m)
        self.assertIsNone(out[0])
        self.assertEqual(out[1], 42.67)
        self.assertIsNone(out[2])


class AlignMonthlyAvailabilityTests(unittest.TestCase):
    def test_rounds_four_decimals(self) -> None:
        periods = pd.period_range("2025-01", "2025-02", freq="M")
        m = {pd.Timestamp("2025-01-01").date(): 99.965432}
        out = _align_monthly_availability_to_list(periods, m)
        self.assertEqual(out[0], 99.9654)
        self.assertIsNone(out[1])


class ChartMonthAxesForPayloadTests(unittest.TestCase):
    def test_end_is_max_of_df_and_availability_max(self) -> None:
        class FakeCur:
            def execute(self, sql: str, params=None) -> None:
                self.last = sql

            def fetchone(self):
                return (pd.Timestamp("2026-06-15").date(),)

        df = pd.DataFrame(
            {
                "month_period": [
                    pd.Period("2025-03", "M"),
                    pd.Period("2026-01", "M"),
                ],
            }
        )
        periods, labels = _chart_month_axes_for_payload(df, FakeCur())
        self.assertEqual(str(periods[0]), "2025-07")
        self.assertEqual(str(periods[-1]), "2026-06")
        self.assertEqual(len(periods), CHART_MONTH_COUNT)
        self.assertEqual(labels[0], "Jul 2025")
        self.assertEqual(labels[-1], "Jun 2026")


class FetchChartsSqlTests(unittest.TestCase):
    def test_queries_use_only_ops_kpi_availability_no_join(self) -> None:
        executed: list[str] = []

        class FakeCur:
            def execute(self, sql: str, params=None) -> None:
                executed.append(sql)

            def fetchall(self):
                return []

        cur = FakeCur()
        periods = pd.period_range(start=CHART_MONTH_START, periods=2, freq="M")
        fetch_monthly_charts_from_availability_only(cur, periods, territory_order=[])

        self.assertEqual(len(executed), 3)
        for sql in executed:
            low = sql.lower()
            self.assertIn("ops_kpi_availability", low)
            self.assertIn("accepted_outage_minutes", low)
            self.assertIn("availability", low)
            self.assertIn("total_available_minutes", low)
            self.assertIn("uptime_per_tenant", low)
            self.assertNotIn(" join ", f" {low} ")

    def test_overall_and_regions_from_fake_rows(self) -> None:
        class FakeCur:
            def __init__(self) -> None:
                self._step = 0

            def execute(self, sql: str, params=None) -> None:
                pass

            def fetchall(self):
                self._step += 1
                if self._step == 1:
                    return [
                        (
                            pd.Timestamp("2025-01-01").date(),
                            10.0,
                            99.5,
                            99.9654,
                        )
                    ]
                if self._step == 2:
                    return [
                        (
                            pd.Timestamp("2025-01-01").date(),
                            "NCR",
                            4.0,
                            88.25,
                            97.1234,
                        )
                    ]
                return []

        periods = pd.period_range(start=CHART_MONTH_START, periods=1, freq="M")
        (
            scope_inc,
            scope_mttr,
            scope_avail,
            terr_inc,
            terr_mttr,
            terr_avail,
        ) = fetch_monthly_charts_from_availability_only(
            FakeCur(), periods, territory_order=["T1"]
        )
        self.assertEqual(scope_inc["Overall"], [10])
        self.assertEqual(scope_mttr["Overall"], [99.5])
        self.assertEqual(scope_avail["Overall"], [99.9654])
        self.assertEqual(scope_inc["NCR"], [4])
        self.assertEqual(scope_mttr["NCR"], [88.25])
        self.assertEqual(scope_avail["NCR"], [97.1234])
        self.assertEqual(terr_inc["T1"], [0])
        self.assertIsNone(terr_mttr["T1"][0])
        self.assertIsNone(terr_avail["T1"][0])


class FetchTableCubesSqlTests(unittest.TestCase):
    def test_cube_queries_use_only_ops_kpi_availability_no_join(self) -> None:
        executed: list[str] = []

        class FakeCur:
            def execute(self, sql: str, params=None) -> None:
                executed.append(sql)

            def fetchall(self):
                return []

        cur = FakeCur()
        fetch_ops_kpi_availability_cubes(cur)

        self.assertEqual(len(executed), 9)
        for sql in executed:
            low = sql.lower()
            self.assertIn("ops_kpi_availability", low)
            self.assertIn("accepted_outage_minutes", low)
            self.assertIn("availability", low)
            self.assertIn("total_available_minutes", low)
            self.assertIn("uptime_per_tenant", low)
            self.assertNotIn(" join ", f" {low} ")

        quarter_queries = [sql for sql in executed if "quarter" in sql.lower()]
        self.assertEqual(len(quarter_queries), 3)


class BuildPeriodOpsIndexQuarterTests(unittest.TestCase):
    def test_quarter_column_maps_to_quarter_kind(self) -> None:
        from operations_kpi_data import build_period_ops_index

        df = pd.DataFrame({"Date": pd.to_datetime(["2026-01-15", "2026-02-15"])})
        periods = {
            "FY2026": df["Date"].dt.year == 2026,
            "Q1_2026": df["Date"].dt.year == 2026,
        }
        index = build_period_ops_index(df, periods)
        self.assertEqual(index["FY2026"], ("fy", 2026))
        self.assertEqual(index["Q1_2026"], ("quarter", (2026, 1)))

    def test_table_ops_actuals_for_row_reads_quarter_cube(self) -> None:
        from operations_kpi_data import OpsKpiFactCubes, table_ops_actuals_for_row

        cubes = OpsKpiFactCubes(
            year_overall={},
            year_region={},
            year_zoo={},
            month_overall={},
            month_region={},
            month_zoo={},
            quarter_overall={(2026, 1): (30.0, 45.5, 99.5)},
            quarter_region={(2026, 1, "NCR"): (10.0, 40.0, 99.9)},
            quarter_zoo={},
        )
        period_ops_index = {
            "Q1_2026": ("quarter", (2026, 1)),
        }
        footer_actuals = table_ops_actuals_for_row(
            cubes, period_ops_index, row_kind="footer", region=None, zoo=None
        )
        self.assertEqual(footer_actuals["Q1_2026"], (30, 45.5, 99.5))

        region_actuals = table_ops_actuals_for_row(
            cubes, period_ops_index, row_kind="region", region="NCR", zoo=None
        )
        self.assertEqual(region_actuals["Q1_2026"], (10, 40.0, 99.9))


if __name__ == "__main__":
    unittest.main()
