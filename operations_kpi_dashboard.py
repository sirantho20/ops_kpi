#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import threading
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
import psycopg

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


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
    env_default_host = os.environ.get("OPERATIONS_KPI_HOST", DEFAULT_HOST)
    env_default_port = _env_int(
        "OPERATIONS_KPI_PORT",
        _env_int("PORT", DEFAULT_PORT),
    )
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
        default=env_default_host,
        help="Host interface to bind.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=env_default_port,
        help="TCP port to bind.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("OPERATIONS_KPI_LOG_LEVEL", "INFO"),
        help="Python logging level (DEBUG, INFO, WARNING, ERROR).",
    )
    parser.add_argument(
        "--debug-errors",
        action="store_true",
        default=_env_flag("OPERATIONS_KPI_DEBUG_ERRORS", default=False),
        help="Expose internal error details in HTTP responses.",
    )
    return parser.parse_args()


def make_handler(
    template_path: Path,
    *,
    database_url: str,
    targets_database_url: str | None,
    debug_errors: bool,
    logger: logging.Logger,
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
        def _error_message(self, exc: Exception, *, fallback: str) -> str:
            return str(exc) if debug_errors else fallback

        def _send_json(
            self,
            payload: dict,
            status: HTTPStatus = HTTPStatus.OK,
            *,
            send_body: bool = True,
        ) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def _serve_healthz(self, send_body: bool = True) -> None:
            body = b'{"status":"ok"}'
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def _serve_readyz(self, send_body: bool = True) -> None:
            try:
                with psycopg.connect(database_url, connect_timeout=3) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        cur.fetchone()
                payload = {"status": "ready"}
                status = HTTPStatus.OK
            except Exception as exc:  # pragma: no cover - network/database dependent
                logger.exception("Readiness check failed")
                payload = {
                    "status": "not_ready",
                    "error": self._error_message(exc, fallback="Database unavailable"),
                }
                status = HTTPStatus.SERVICE_UNAVAILABLE
            self._send_json(payload, status=status, send_body=send_body)

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
                logger.exception("Unexpected cell insight failure")
                self._send_json(
                    {
                        "error": self._error_message(
                            exc, fallback="Internal server error"
                        )
                    },
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )

        def _serve_dashboard(self, send_body: bool) -> None:
            try:
                data_fp = ops_kpi_data_fingerprint(database_url)
                st = template_path.stat()
                html = rendered_html(
                    data_fp, str(template_path.resolve()), st.st_mtime
                )
            except Exception as exc:  # pragma: no cover - surfaced in browser and console
                logger.exception("Failed to render dashboard")
                self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                if send_body:
                    message = self._error_message(
                        exc,
                        fallback="Dashboard failed to load.",
                    )
                    self.wfile.write(message.encode("utf-8"))
                return

            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                self._serve_healthz(send_body=True)
                return
            if parsed.path == "/readyz":
                self._serve_readyz(send_body=True)
                return
            if parsed.path == "/api/cell-insight":
                self._serve_cell_insight()
                return
            if parsed.path not in {"/", "/index.html"}:
                self.send_error(HTTPStatus.NOT_FOUND, "Page not found")
                return
            self._serve_dashboard(send_body=True)

        def do_HEAD(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                self._serve_healthz(send_body=False)
                return
            if parsed.path == "/readyz":
                self._serve_readyz(send_body=False)
                return
            if parsed.path == "/api/cell-insight":
                self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
                self.end_headers()
                return
            if parsed.path not in {"/", "/index.html"}:
                self.send_error(HTTPStatus.NOT_FOUND, "Page not found")
                return
            self._serve_dashboard(send_body=False)

        def log_message(self, format: str, *args) -> None:
            logger.info("%s - %s", self.address_string(), format % args)

    return DashboardHandler


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    logger = logging.getLogger("operations_kpi_dashboard")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error(
            "DATABASE_URL is not set. Add it to .env or the environment (PostgreSQL connection string).",
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
        debug_errors=args.debug_errors,
        logger=logger,
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)

    logger.info("Serving Operations KPI dashboard at http://%s:%s", args.host, args.port)
    logger.info("Data source: PostgreSQL (DATABASE_URL)")
    logger.info("HTML template: %s", template_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down dashboard server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
