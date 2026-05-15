#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import threading
import time
import urllib.request
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
import psycopg
import psycopg.errors

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


def _database_error_page(
    *,
    error_kind: str,
    debug_detail: str | None = None,
) -> bytes:
    detail = ""
    if debug_detail:
        detail = f"<pre>{html.escape(debug_detail)}</pre>"
    if error_kind == "permission":
        title = "Operations KPI — database permission denied"
        lead = (
            "PostgreSQL accepted the connection, but the <code>DATABASE_URL</code> role "
            "<strong>cannot read</strong> the KPI fact tables (for example <code>ops_kpi_availability</code>)."
        )
        bullets = (
            "<li>Grant <code>USAGE</code> on the relevant schema and <code>SELECT</code> on "
            "<code>ops_kpi_availability</code>, <code>ops_kpi_sic</code>, "
            "<code>ops_kpi_site_visit</code> (or configured alias), <code>ops_kpi_cm</code>, "
            "<code>ops_kpi_targets</code>, and <code>site</code> (or use a role that already has them).</li>"
            "<li>Retry after permissions are updated (no code change required).</li>"
        )
    elif error_kind == "schema":
        title = "Operations KPI — data shape needs attention"
        lead = (
            "The dashboard connected to PostgreSQL, but the KPI data shape did not match "
            "what the dashboard expects."
        )
        bullets = (
            "<li>Check required tables/columns for <code>ops_kpi_sic</code> and a site-visit "
            "table (for example <code>ops_kpi_site_visit</code>; see loader discovery order).</li>"
            "<li>SIC needs a recognizable site column and date column; ticket-based tables "
            "can be counted with <code>ticket_id</code>.</li>"
            "<li>Restart or reload after the table shape is corrected.</li>"
        )
    elif error_kind == "query":
        title = "Operations KPI — database query failed"
        lead = (
            "PostgreSQL accepted the connection, but a dashboard query failed while loading "
            "the KPI payload."
        )
        bullets = (
            "<li>Verify that required tables exist: <code>site</code>, "
            "<code>ops_kpi_availability</code>, <code>ops_kpi_sic</code>, a site-visit facts table "
            "(e.g. <code>ops_kpi_site_visit</code>), "
            "<code>ops_kpi_cm</code>, and <code>ops_kpi_targets</code>.</li>"
            "<li>Check that the live schema matches the dashboard loader expectations.</li>"
        )
    else:
        title = "Operations KPI — database unavailable"
        lead = (
            "The HTTP server is running, but <strong>PostgreSQL could not be reached</strong> "
            "using <code>DATABASE_URL</code>."
        )
        bullets = (
            "<li>Ensure Postgres is running and accepting connections.</li>"
            "<li>Set <code>DATABASE_URL</code> in <code>.env</code> or your environment.</li>"
        )
    page_html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><title>{html.escape(title)}</title></head>
<body style="font-family:system-ui,sans-serif;max-width:42rem;margin:2rem;line-height:1.5">
<h1>Operations KPI Dashboard</h1>
<p>{lead}</p>
<ul>
{bullets}
<li>Process check: <a href="/healthz"><code>/healthz</code></a></li>
<li>DB ping: <a href="/readyz"><code>/readyz</code></a></li>
</ul>
{detail}
</body></html>"""
    return page_html.encode("utf-8")


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
    # Repo .env then CWD .env so DATABASE_URL resolves whether you launch from ~/ops_kpi or elsewhere.
    load_dotenv(ROOT / ".env")
    load_dotenv()
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
    parser.add_argument(
        "--no-prewarm",
        dest="prewarm",
        action="store_false",
        help="Disable background warm-up of the / page (first browser request can take many minutes).",
    )
    parser.set_defaults(prewarm=_env_flag("OPERATIONS_KPI_PREWARM", default=True))
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
                    database_url=database_url,
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
            except psycopg.Error as exc:  # pragma: no cover - database dependent
                logger.exception("Dashboard unavailable: database error")
                detail = self._error_message(exc, fallback="")
                if isinstance(exc, psycopg.errors.InsufficientPrivilege):
                    kind = "permission"
                elif isinstance(exc, psycopg.OperationalError):
                    kind = "connect"
                else:
                    kind = "query"
                body = _database_error_page(
                    error_kind=kind,
                    debug_detail=detail if debug_errors else None,
                )
                self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if send_body:
                    self.wfile.write(body)
                return
            except ValueError as exc:
                logger.exception("Dashboard unavailable: data shape error")
                detail = self._error_message(exc, fallback="")
                body = _database_error_page(
                    error_kind="schema",
                    debug_detail=detail if debug_errors else None,
                )
                self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if send_body:
                    self.wfile.write(body)
                return
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


def _prewarm_dashboard_cache(*, logger: logging.Logger, port: int) -> None:
    """Request / on loopback so the heavy DB render runs once and lru_cache stays hot."""
    time.sleep(0.35)
    deadline_s = float(os.environ.get("OPERATIONS_KPI_PREWARM_TIMEOUT", "900"))
    url = f"http://127.0.0.1:{port}/"
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=deadline_s) as resp:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
        logger.info(
            "Dashboard pre-warm finished in %.1fs; / should respond quickly for browsers now.",
            time.perf_counter() - t0,
        )
    except Exception:
        logger.exception(
            "Dashboard pre-warm failed; the first / request may take a long time.",
        )


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
            "DATABASE_URL is not set. Add it to %s, a .env in your current directory, "
            "or export DATABASE_URL (PostgreSQL connection string).",
            ROOT / ".env",
        )
        logger.error(
            "Without it the HTTP server never starts, so nothing will listen on the port.",
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
    serve_thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name="operations-kpi-http",
    )
    serve_thread.start()

    logger.info("Serving Operations KPI dashboard at http://%s:%s", args.host, args.port)
    lo = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    logger.info(
        "Sanity check (no DB required): curl http://%s:%s/healthz",
        lo,
        args.port,
    )
    logger.info("Data source: PostgreSQL (DATABASE_URL)")
    logger.info("HTML template: %s", template_path)
    if args.prewarm:
        logger.info(
            "Pre-warming dashboard in the background (avoids a multi-minute blank wait on first / in the browser).",
        )
        threading.Thread(
            target=_prewarm_dashboard_cache,
            kwargs={"logger": logger, "port": args.port},
            daemon=True,
            name="operations-kpi-prewarm",
        ).start()
    try:
        serve_thread.join()
    except KeyboardInterrupt:
        logger.info("Shutting down dashboard server.")
    finally:
        server.shutdown()
        server.server_close()
        serve_thread.join(timeout=15)


if __name__ == "__main__":
    main()
