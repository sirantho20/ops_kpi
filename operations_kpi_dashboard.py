#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

from operations_kpi_data import (
    OpsKpiTargets,
    build_periods,
    load_daily_availability_from_database,
    load_dashboard_payload,
    load_ops_kpi_targets,
    ops_kpi_data_fingerprint,
)
from operations_kpi_insights import METRICS, ROW_KINDS, compute_cell_insight


ROOT = Path(__file__).resolve().parent
DEFAULT_TEMPLATE = ROOT / "Operations KPI.html"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8054
DATA_PLACEHOLDER = "__DASHBOARD_DATA__"

_analysis_cache: dict[str, tuple[str, object, dict, OpsKpiTargets]] = {}
_analysis_lock = threading.Lock()


def get_analysis_context(
    *,
    database_url: str,
    targets_database_url: str | None,
):
    """Load and cache df, periods, and OpsKpiTargets; invalidate when data or targets change."""
    fp = ops_kpi_data_fingerprint(database_url)
    key = f"{database_url}|{targets_database_url or ''}"
    with _analysis_lock:
        entry = _analysis_cache.get(key)
        if entry is not None and entry[0] == fp:
            return entry[1], entry[2], entry[3]
    df = load_daily_availability_from_database(database_url)
    periods = build_periods(df)
    tgt_url = targets_database_url if targets_database_url is not None else database_url
    targets = load_ops_kpi_targets(tgt_url)
    with _analysis_lock:
        _analysis_cache[key] = (fp, df, periods, targets)
    return df, periods, targets


def parse_args() -> argparse.Namespace:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(
        description="Serve the Operations KPI dashboard (PostgreSQL only)."
    )
    parser.add_argument(
        "--template",
        default=str(DEFAULT_TEMPLATE),
        help="Path to the HTML dashboard template.",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="Host interface to bind.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="TCP port to bind.",
    )
    return parser.parse_args()


def make_handler(
    template_path: Path,
    *,
    database_url: str,
    targets_database_url: str | None,
):
    @lru_cache(maxsize=32)
    def rendered_html(data_fp: str, template_path_str: str, template_mtime: float) -> str:
        tpl = Path(template_path_str)
        html_template = tpl.read_text(encoding="utf-8")
        if DATA_PLACEHOLDER not in html_template:
            raise ValueError(
                f"Template {tpl} is missing {DATA_PLACEHOLDER!r}."
            )
        payload = load_dashboard_payload(
            database_url,
            targets_database_url=targets_database_url,
        )
        return html_template.replace(
            DATA_PLACEHOLDER, json.dumps(payload, separators=(",", ":"))
        )

    class DashboardHandler(BaseHTTPRequestHandler):
        def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_cell_insight(self) -> None:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query, keep_blank_values=True)

            def first(key: str) -> str | None:
                vals = qs.get(key)
                if not vals:
                    return None
                return vals[0] if vals[0] != "" else None

            try:
                row_kind = first("rowKind")
                region = first("region")
                metric = first("metric")
                period = first("period")
                zoo = first("zoo")
                if not row_kind or not region or not metric or not period:
                    raise ValueError(
                        "Required query parameters: rowKind, region, metric, period"
                    )
                if row_kind not in ROW_KINDS:
                    raise ValueError(f"Invalid rowKind: {row_kind}")
                if metric not in METRICS:
                    raise ValueError(f"Invalid metric: {metric}")
                if row_kind == "zoo" and not zoo:
                    raise ValueError("zoo is required when rowKind is zoo")
                if row_kind != "zoo":
                    zoo = None
                df, periods, ops_targets = get_analysis_context(
                    database_url=database_url,
                    targets_database_url=targets_database_url,
                )
                payload = compute_cell_insight(
                    df,
                    periods,
                    row_kind,
                    region,
                    zoo,
                    metric,
                    period,
                    ops_targets,
                )
                self._send_json(payload)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as exc:  # pragma: no cover
                self._send_json(
                    {"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR
                )

        def _serve_dashboard(self, send_body: bool) -> None:
            try:
                data_fp = ops_kpi_data_fingerprint(database_url)
                st = template_path.stat()
                html = rendered_html(
                    data_fp, str(template_path.resolve()), st.st_mtime
                )
            except Exception as exc:  # pragma: no cover - surfaced in browser and console
                self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                if send_body:
                    self.wfile.write(f"Dashboard failed to load:\n{exc}".encode("utf-8"))
                raise

            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/cell-insight":
                self._serve_cell_insight()
                return
            if parsed.path not in {"/", "/index.html"}:
                self.send_error(HTTPStatus.NOT_FOUND, "Page not found")
                return
            self._serve_dashboard(send_body=True)

        def do_HEAD(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/cell-insight":
                self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
                self.end_headers()
                return
            if parsed.path not in {"/", "/index.html"}:
                self.send_error(HTTPStatus.NOT_FOUND, "Page not found")
                return
            self._serve_dashboard(send_body=False)

        def log_message(self, format: str, *args) -> None:
            print(f"{self.address_string()} - {format % args}")

    return DashboardHandler


def main() -> None:
    args = parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print(
            "DATABASE_URL is not set. Add it to .env or the environment (PostgreSQL connection string).",
            file=sys.stderr,
        )
        raise SystemExit(1)
    targets_database_url = database_url
    template_path = Path(args.template).resolve()

    if not template_path.is_file():
        raise FileNotFoundError(f"Template not found: {template_path}")

    handler = make_handler(
        template_path,
        database_url=database_url,
        targets_database_url=targets_database_url,
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)

    print(f"Serving Operations KPI dashboard at http://{args.host}:{args.port}")
    print("Data source: PostgreSQL (DATABASE_URL)")
    print(f"HTML template: {template_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down dashboard server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
