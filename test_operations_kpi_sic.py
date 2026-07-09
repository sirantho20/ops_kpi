from __future__ import annotations

import unittest

import pandas as pd

from operations_kpi_data import (
    OpsKpiSicColumns,
    OpsKpiSiteVisitColumns,
    VISIT_TABLE_FY_YEARS,
    VISIT_TABLE_TOTAL_PERIOD_KEY,
    _ops_kpi_load_sql,
    build_meta,
    build_site_visit_table_periods,
    build_table_row,
    build_visit_compact_periods,
    default_ops_kpi_targets,
    detect_ops_kpi_sic_columns,
    fy_key,
    group_month_periods_for_table,
    period_key,
    site_visit_table_month_periods,
    site_visit_table_period_order,
)


class FakeColumnsCursor:
    def __init__(self, columns: list[str]) -> None:
        self.columns = columns

    def execute(self, sql: str, params=None) -> None:
        self.sql = sql
        self.params = params

    def fetchall(self):
        return [(c,) for c in self.columns]


class OpsKpiSicColumnDetectionTests(unittest.TestCase):
    def test_detects_conventional_sic_columns(self) -> None:
        detected = detect_ops_kpi_sic_columns(
            FakeColumnsCursor(["site_id", "date", "sic_count"])
        )
        self.assertEqual(detected.site_column, "site_id")
        self.assertEqual(detected.site_join_dimension, "site_table_site_id")
        self.assertEqual(detected.date_column, "date")
        self.assertEqual(detected.value_column, "sic_count")
        self.assertEqual(detected.value_mode, "sum")

    def test_detects_pla_based_sic_columns(self) -> None:
        detected = detect_ops_kpi_sic_columns(
            FakeColumnsCursor(["pla_id", "sic_date", "sic"])
        )
        self.assertEqual(detected.site_column, "pla_id")
        self.assertEqual(detected.site_join_dimension, "kpi_site_id")
        self.assertEqual(detected.date_column, "sic_date")
        self.assertEqual(detected.value_column, "sic")
        self.assertEqual(detected.value_mode, "sum")

    def test_detects_ticket_based_live_sic_shape(self) -> None:
        detected = detect_ops_kpi_sic_columns(
            FakeColumnsCursor(
                [
                    "site_id",
                    "outage_start",
                    "ticket_id",
                    "raw_outage_duration",
                    "accepted_outage_duration",
                    "rca",
                    "rca_subcategory",
                ]
            )
        )
        self.assertEqual(detected.site_column, "site_id")
        self.assertEqual(detected.site_join_dimension, "site_table_site_id")
        self.assertEqual(detected.date_column, "outage_start")
        self.assertEqual(detected.value_column, "ticket_id")
        self.assertEqual(detected.value_mode, "distinct_count")

    def test_rejects_unknown_shape(self) -> None:
        with self.assertRaisesRegex(ValueError, "site and date"):
            detect_ops_kpi_sic_columns(FakeColumnsCursor(["foo", "bar"]))


class OpsKpiSicSqlTests(unittest.TestCase):
    def test_load_sql_uses_sic_table_and_count(self) -> None:
        sql = _ops_kpi_load_sql(
            has_ops_kpi_cm_count=True,
            sic_columns=OpsKpiSicColumns(
                site_column="site_id",
                site_join_dimension="site_table_site_id",
                date_column="date",
                value_column="sic_count",
            ),
        )
        self.assertIn("ops_kpi_sic", sql)
        self.assertIn('"SIC Count"', sql)
        self.assertIn('"Site Visit Count"', sql)
        self.assertIn("0::integer", sql)

    def test_load_sql_includes_site_visit_when_configured(self) -> None:
        sql = _ops_kpi_load_sql(
            has_ops_kpi_cm_count=True,
            sic_columns=OpsKpiSicColumns(
                site_column="site_id",
                site_join_dimension="site_table_site_id",
                date_column="date",
                value_column="sic_count",
            ),
            site_visit_columns=OpsKpiSiteVisitColumns(
                table_name="ops_kpi_site_visit",
                site_column="site_id",
                site_join_dimension="site_table_site_id",
                date_column="date",
                value_column="visit_count",
            ),
        )
        self.assertIn("site_visit_counts", sql)
        self.assertIn('public."ops_kpi_site_visit"', sql)
        self.assertIn("COALESCE(svc.site_visit_count", sql)
        self.assertNotIn('0::integer AS "Site Visit Count"', sql)

    def test_load_sql_counts_distinct_ticket_ids(self) -> None:
        sql = _ops_kpi_load_sql(
            has_ops_kpi_cm_count=True,
            sic_columns=OpsKpiSicColumns(
                site_column="site_id",
                site_join_dimension="site_table_site_id",
                date_column="outage_start",
                value_column="ticket_id",
                value_mode="distinct_count",
            ),
        )
        self.assertIn(
            'COUNT(DISTINCT NULLIF(BTRIM(sic."ticket_id"::text), \'\'))::integer AS sic_count',
            sql,
        )
        self.assertIn('SELECT sic."outage_start"::date AS dt FROM ops_kpi_sic', sql)


class SicTableTargetTests(unittest.TestCase):
    def test_meta_visit_has_no_target_column_site_visit_keeps_it(self) -> None:
        df = pd.DataFrame({"Date": pd.to_datetime(["2025-01-01", "2026-01-01"])})
        periods = {
            fy_key(2025): df["Date"].dt.year == 2025,
            fy_key(2026): df["Date"].dt.year == 2026,
        }
        meta = build_meta(df, periods, default_ops_kpi_targets())
        visit_order = meta["metricPeriodOrder"]["visit"]
        site_visit_order = meta["metricPeriodOrder"]["siteVisit"]
        self.assertNotIn("TARGET", visit_order)
        self.assertNotIn(VISIT_TABLE_TOTAL_PERIOD_KEY, site_visit_order)
        self.assertEqual(site_visit_order, site_visit_table_period_order())
        self.assertEqual(site_visit_order[-1], "TARGET")
        self.assertEqual(
            visit_order,
            [
                fy_key(VISIT_TABLE_FY_YEARS[0]),
                fy_key(VISIT_TABLE_FY_YEARS[1]),
                VISIT_TABLE_TOTAL_PERIOD_KEY,
            ],
        )

    def test_table_row_visit_has_no_target_cell(self) -> None:
        df = pd.DataFrame(
            {
                "Date": pd.to_datetime(["2024-06-01", "2025-06-01", "2026-06-01"]),
                "SIC Count": [1, 2, 3],
                "CM Count": [0, 0, 0],
                "Site Visit Count": [0, 0, 0],
                "Incident_count": [0, 0, 0],
                "Accepted Outage Minutes": [0, 0, 0],
                "has_availability_row": [False, False, False],
            }
        )
        periods = {
            fy_key(2025): df["Date"].dt.year == 2025,
            fy_key(2026): df["Date"].dt.year == 2026,
        }
        df["month_period"] = df["Date"].dt.to_period("M")
        row = build_table_row(df, periods, default_ops_kpi_targets())
        self.assertNotIn("TARGET", row["visit"])
        self.assertNotIn(VISIT_TABLE_TOTAL_PERIOD_KEY, row["siteVisit"])
        self.assertIn("TARGET", row["siteVisit"])
        for column_key, _ in group_month_periods_for_table(
            site_visit_table_month_periods(df)
        ):
            self.assertIn(column_key, row["siteVisit"])


class SiteVisitTablePeriodTests(unittest.TestCase):
    def test_site_visit_periods_have_fy_and_available_months_no_total(self) -> None:
        df = pd.DataFrame(
            {
                "Date": pd.to_datetime(
                    ["2025-06-01", "2026-01-15", "2026-02-20", "2026-03-10"]
                ),
            }
        )
        df["month_period"] = df["Date"].dt.to_period("M")
        periods = build_site_visit_table_periods(df)
        self.assertNotIn(VISIT_TABLE_TOTAL_PERIOD_KEY, periods)
        self.assertIn(fy_key(2025), periods)
        self.assertIn(fy_key(2026), periods)
        for month_period in site_visit_table_month_periods(df):
            key = period_key(month_period)
            self.assertIn(key, periods)
            expected = df["month_period"] == month_period
            self.assertTrue(periods[key].equals(expected))

    def test_site_visit_month_periods_extend_past_old_q1_cutoff(self) -> None:
        """Regression test: columns must track the latest month in the data,
        not stop at March, as the database gains new months (e.g. June)."""
        df = pd.DataFrame(
            {
                "Date": pd.to_datetime(
                    ["2026-01-15", "2026-04-01", "2026-05-01", "2026-06-01"]
                ),
            }
        )
        df["month_period"] = df["Date"].dt.to_period("M")
        month_periods = site_visit_table_month_periods(df)
        self.assertEqual(
            [period_key(p) for p in month_periods],
            ["JAN_26", "FEB_26", "MAR_26", "APR_26", "MAY_26", "JUN_26"],
        )
        periods = build_site_visit_table_periods(df)
        self.assertIn("JUN_26", periods)
        self.assertTrue(periods["JUN_26"].equals(df["month_period"] == pd.Period("2026-06", freq="M")))


class GroupMonthPeriodsForTableTests(unittest.TestCase):
    def test_collapses_oldest_complete_quarter_once_over_threshold(self) -> None:
        """Jan-Jun (6 months, > 5) collapses Jan/Feb/Mar into Q1_2026 while
        Apr/May/Jun stay as individual columns, per the requested behavior."""
        months = pd.period_range("2026-01", "2026-06", freq="M")
        groups = group_month_periods_for_table(months)
        self.assertEqual(
            [key for key, _ in groups],
            ["Q1_2026", "APR_26", "MAY_26", "JUN_26"],
        )
        self.assertEqual(groups[0][1], tuple(months[0:3]))

    def test_no_collapse_at_or_under_threshold(self) -> None:
        months = pd.period_range("2026-01", "2026-05", freq="M")
        groups = group_month_periods_for_table(months)
        self.assertEqual(
            [key for key, _ in groups],
            ["JAN_26", "FEB_26", "MAR_26", "APR_26", "MAY_26"],
        )

    def test_collapses_second_quarter_when_still_over_threshold(self) -> None:
        months = pd.period_range("2026-01", "2026-09", freq="M")
        groups = group_month_periods_for_table(months)
        self.assertEqual(
            [key for key, _ in groups],
            ["Q1_2026", "Q2_2026", "JUL_26", "AUG_26", "SEP_26"],
        )


class BuildPeriodsQuarterCollapseTests(unittest.TestCase):
    def test_build_periods_collapses_current_year_months(self) -> None:
        from operations_kpi_data import build_periods

        df = pd.DataFrame(
            {
                "Date": pd.to_datetime(
                    [
                        "2025-03-01",
                        "2026-01-01",
                        "2026-02-01",
                        "2026-03-01",
                        "2026-04-01",
                        "2026-05-01",
                        "2026-06-01",
                    ]
                )
            }
        )
        df["month_period"] = df["Date"].dt.to_period("M")
        periods = build_periods(df)
        self.assertEqual(
            list(periods.keys()),
            ["FY2025", "FY2026", "Q1_2026", "APR_26", "MAY_26", "JUN_26"],
        )
        self.assertEqual(int(periods["Q1_2026"].sum()), 3)


class VisitCompactPeriodTests(unittest.TestCase):
    def test_total_equals_union_of_fy_masks(self) -> None:
        df = pd.DataFrame(
            {
                "Date": pd.to_datetime(
                    ["2025-06-01", "2026-03-15", "2027-01-01", "2024-12-01"]
                ),
            }
        )
        p = build_visit_compact_periods(df)
        combo = p["FY2025"] | p["FY2026"]
        self.assertTrue((p[VISIT_TABLE_TOTAL_PERIOD_KEY] == combo).all())


if __name__ == "__main__":
    unittest.main()
