from __future__ import annotations

import unittest

import pandas as pd

from operations_kpi_data import (
    OpsKpiSicColumns,
    OpsKpiSiteVisitColumns,
    VISIT_TABLE_TOTAL_PERIOD_KEY,
    _ops_kpi_load_sql,
    build_visit_compact_periods,
    detect_ops_kpi_sic_columns,
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
