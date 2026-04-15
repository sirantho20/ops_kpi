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


if __name__ == "__main__":
    unittest.main()
