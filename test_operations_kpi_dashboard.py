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

import operations_kpi_dashboard as dashboard


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
        with mock.patch.object(
            dashboard.psycopg,
            "connect",
            side_effect=RuntimeError("private connection details"),
        ):
            with run_test_server() as base_url:
                status, body = fetch_json(f"{base_url}/readyz")

        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "not_ready")
        self.assertEqual(body["error"], "Database unavailable")

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


if __name__ == "__main__":
    unittest.main()
