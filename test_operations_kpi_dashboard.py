from __future__ import annotations

import contextlib
import json
import logging
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from http.server import ThreadingHTTPServer

import pandas as pd
import operations_kpi_dashboard as dashboard
import operations_kpi_data as data
from test_operations_kpi_insights import _sample_df


@contextlib.contextmanager
def run_test_server(*, debug_errors: bool = False):
    with TemporaryDirectory() as temp_dir:
        template_path = Path(temp_dir) / "template.html"
        template_path.write_text(
            "<html><body>__DASHBOARD_DATA__</body></html>",
            encoding="utf-8",
        )
        handler = dashboard.make_handler(
            template_path,
            database_url="postgres://unused",
            targets_database_url=None,
            debug_errors=debug_errors,
            logger=logging.getLogger("test.operations_kpi_dashboard"),
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def fetch_json(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            body = response.read()
            return response.status, json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def fetch_error_text(url: str) -> tuple[int, str, str]:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            exc.headers.get("Content-Type", ""),
            exc.read().decode("utf-8"),
        )


class DashboardServerRoutesTests(unittest.TestCase):
    def test_healthz_returns_ok(self) -> None:
        with run_test_server() as base_url:
            status, body = fetch_json(f"{base_url}/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_readyz_returns_ready_when_db_ping_succeeds(self) -> None:
        connect_ctx = mock.MagicMock()
        conn = mock.MagicMock()
        cur = mock.MagicMock()
        connect_ctx.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cur

        with mock.patch.object(dashboard.psycopg, "connect", return_value=connect_ctx):
            with run_test_server() as base_url:
                status, body = fetch_json(f"{base_url}/readyz")

        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ready"})
        cur.execute.assert_called_once_with("SELECT 1")

    def test_readyz_hides_internal_error_when_db_ping_fails(self) -> None:
        test_logger = logging.getLogger("test.operations_kpi_dashboard")
        with mock.patch.object(
            dashboard.psycopg,
            "connect",
            side_effect=RuntimeError("private connection details"),
        ):
            with self.assertLogs(test_logger, level="WARNING") as captured:
                with run_test_server() as base_url:
                    status, body = fetch_json(f"{base_url}/readyz")

        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "not_ready")
        self.assertEqual(body["error"], "Database unavailable")
        self.assertTrue(
            any("Readiness check failed" in line for line in captured.output)
        )

    def test_cell_insight_bad_row_kind_logs_warning(self) -> None:
        test_logger = logging.getLogger("test.operations_kpi_dashboard")
        with self.assertLogs(test_logger, level="WARNING") as captured:
            with run_test_server() as base_url:
                status, body = fetch_json(
                    f"{base_url}/api/cell-insight?"
                    "rowKind=invalid&region=NCR&metric=events&period=Jun_25"
                )
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        self.assertTrue(
            any("cell insight bad request" in line.lower() for line in captured.output)
        )

    def test_dashboard_route_hides_internal_error_when_debug_off(self) -> None:
        with mock.patch.object(
            dashboard,
            "ops_kpi_data_fingerprint",
            side_effect=RuntimeError("private SQL detail"),
        ):
            with run_test_server(debug_errors=False) as base_url:
                with self.assertRaises(urllib.error.HTTPError) as exc_info:
                    urllib.request.urlopen(f"{base_url}/", timeout=3)
        self.assertEqual(exc_info.exception.code, 500)
        body = exc_info.exception.read().decode("utf-8")
        self.assertEqual(body, "Dashboard failed to load.")

    def test_dashboard_route_shows_internal_error_when_debug_on(self) -> None:
        with mock.patch.object(
            dashboard,
            "ops_kpi_data_fingerprint",
            side_effect=RuntimeError("private SQL detail"),
        ):
            with run_test_server(debug_errors=True) as base_url:
                with self.assertRaises(urllib.error.HTTPError) as exc_info:
                    urllib.request.urlopen(f"{base_url}/", timeout=3)
        self.assertEqual(exc_info.exception.code, 500)
        body = exc_info.exception.read().decode("utf-8")
        self.assertIn("private SQL detail", body)

    def test_dashboard_route_returns_html_for_database_query_error(self) -> None:
        with mock.patch.object(
            dashboard,
            "ops_kpi_data_fingerprint",
            side_effect=dashboard.psycopg.ProgrammingError("relation missing"),
        ):
            with run_test_server(debug_errors=False) as base_url:
                status, content_type, body = fetch_error_text(f"{base_url}/")

        self.assertEqual(status, 503)
        self.assertIn("text/html", content_type)
        self.assertIn("database query failed", body)
        self.assertIn("ops_kpi_sic", body)
        self.assertIn("ops_kpi_site_visit", body)
        self.assertNotIn("relation missing", body)

    def test_dashboard_route_returns_html_for_sic_schema_error(self) -> None:
        with mock.patch.object(dashboard, "ops_kpi_data_fingerprint", return_value="fp"):
            with mock.patch.object(
                dashboard,
                "load_dashboard_payload",
                side_effect=ValueError("Table public.ops_kpi_sic must include recognizable site and date columns."),
            ):
                with run_test_server(debug_errors=False) as base_url:
                    status, content_type, body = fetch_error_text(f"{base_url}/")

        self.assertEqual(status, 503)
        self.assertIn("text/html", content_type)
        self.assertIn("data shape", body)
        self.assertIn("ops_kpi_sic", body)
        self.assertNotIn("recognizable site and date", body)

    def test_dashboard_route_debug_shows_sic_schema_error_detail(self) -> None:
        detail = "Table public.ops_kpi_sic must include recognizable site and date columns."
        with mock.patch.object(dashboard, "ops_kpi_data_fingerprint", return_value="fp"):
            with mock.patch.object(
                dashboard,
                "load_dashboard_payload",
                side_effect=ValueError(detail),
            ):
                with run_test_server(debug_errors=True) as base_url:
                    status, content_type, body = fetch_error_text(f"{base_url}/")

        self.assertEqual(status, 503)
        self.assertIn("text/html", content_type)
        self.assertIn(detail, body)


class CellInsightExportRouteTests(unittest.TestCase):
    def test_export_returns_csv_attachment(self) -> None:
        df = _sample_df()
        periods = {
            "Jun_25": (
                (df["Date"] == pd.Timestamp("2025-06-01"))
                | (df["Date"] == pd.Timestamp("2025-06-02"))
            ),
        }
        targets = data.default_ops_kpi_targets()
        with mock.patch.object(
            dashboard,
            "get_analysis_context",
            return_value=(df, periods, targets),
        ):
            with run_test_server() as base_url:
                url = (
                    f"{base_url}/api/cell-insight/export?"
                    "rowKind=region&region=NCR&metric=events&period=Jun_25"
                )
                with urllib.request.urlopen(url, timeout=3) as response:
                    body = response.read().decode("utf-8")
                    content_type = response.headers.get("Content-Type", "")
                    disposition = response.headers.get("Content-Disposition", "")

        self.assertEqual(response.status, 200)
        self.assertIn("text/csv", content_type)
        self.assertIn("attachment", disposition)
        self.assertIn("ops_kpi_events_NCR_Jun_25.csv", disposition)
        self.assertIn("Date", body.splitlines()[0])
        self.assertIn("Incident_count", body.splitlines()[0])
        self.assertGreaterEqual(len(body.splitlines()), 3)

    def test_export_target_returns_bad_request(self) -> None:
        df = _sample_df()
        periods = {"Jun_25": df["Date"].notna()}
        targets = data.default_ops_kpi_targets()
        with mock.patch.object(
            dashboard,
            "get_analysis_context",
            return_value=(df, periods, targets),
        ):
            with run_test_server() as base_url:
                url = (
                    f"{base_url}/api/cell-insight/export?"
                    "rowKind=region&region=NCR&metric=events&period=TARGET"
                )
                status, body = fetch_json(url)

        self.assertEqual(status, 400)
        self.assertIn("TARGET", body["error"])


class DashboardTemplateTests(unittest.TestCase):
    def test_sic_is_table_only_not_chart_type(self) -> None:
        template = dashboard.DEFAULT_TEMPLATE.read_text(encoding="utf-8")
        metric_configs = template.split("const metricConfigs = [", 1)[1].split(
            "];", 1
        )[0]
        chart_types = template.split("const chartTypes = [", 1)[1].split("];", 1)[0]

        self.assertIn("id: 'visit'", metric_configs)
        self.assertIn("title: 'SIC'", metric_configs)
        self.assertNotIn("id: 'visit'", chart_types)
        self.assertNotIn("SIC", chart_types)
        self.assertIn("id: 'siteVisit'", chart_types)
        self.assertIn("Site Visit Count", chart_types)
        self.assertIn("grid-cols-5", template)
        self.assertNotIn("grid-cols-4", template)


class DashboardChartPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        rows = []
        for region in data.CHART_ROW_ORDER:
            site_visit_by_date = {
                "2025-01-01": 10 if region == "NCR" else 0,
                "2025-02-01": 20 if region == "NCR" else 0,
                "2026-01-01": 7 if region == "NCR" else 0,
                "2026-02-01": 11 if region == "NCR" else 0,
            }
            for date_text, site_visit_count in site_visit_by_date.items():
                date_value = pd.Timestamp(date_text)
                rows.append(
                    {
                        "Date": date_value,
                        "month_period": date_value.to_period("M"),
                        "Region": region,
                        "territory_chart_group": "Metro" if region == "NCR" else region,
                        "Incident_count": 0,
                        "CM Count": 0,
                        "Site Visit Count": site_visit_count,
                    }
                )
        self.df = pd.DataFrame(rows)
        self.periods = {
            "FY2025": self.df["Date"].dt.year == 2025,
            "FY2026": self.df["Date"].dt.year == 2026,
        }
        self.month_periods = pd.period_range("2026-01", "2026-02", freq="M")
        self.month_labels = ["Jan 2026", "Feb 2026"]
        self.targets = data.default_ops_kpi_targets()

    def test_build_charts_includes_site_visit_series(self) -> None:
        charts = data.build_charts(
            self.df,
            self.periods,
            self.targets,
            events_month_periods=self.month_periods,
            events_month_labels=self.month_labels,
            events_actuals_by_scope={scope: [0, 0] for scope in data.CHART_ROW_ORDER},
            mttr_actuals_by_scope={scope: [None, None] for scope in data.CHART_ROW_ORDER},
            availability_actuals_by_scope={
                scope: [None, None] for scope in data.CHART_ROW_ORDER
            },
        )

        site_visit = charts["NCR"]["siteVisit"]
        self.assertTrue(site_visit["available"])
        self.assertEqual(site_visit["months"], self.month_labels)
        self.assertEqual(site_visit["actual"], [7, 11])
        self.assertEqual(site_visit["target"], [13, 13])

    def test_build_territory_charts_includes_site_visit_series(self) -> None:
        territory_order, territory_charts = data.build_territory_charts(
            self.df,
            self.periods,
            self.targets,
            events_month_periods=self.month_periods,
            events_month_labels=self.month_labels,
            events_actuals_by_territory={
                territory: [0, 0]
                for territory in self.df["territory_chart_group"].unique()
            },
            mttr_actuals_by_territory={
                territory: [None, None]
                for territory in self.df["territory_chart_group"].unique()
            },
            availability_actuals_by_territory={
                territory: [None, None]
                for territory in self.df["territory_chart_group"].unique()
            },
        )

        self.assertIn("Metro", territory_order)
        site_visit = territory_charts["Metro"]["siteVisit"]
        self.assertEqual(site_visit["actual"], [7, 11])
        self.assertEqual(site_visit["target"], [13, 13])


if __name__ == "__main__":
    unittest.main()
