#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import binascii
import contextvars
import html
import json
import logging
import os
import secrets
import threading
import time
import urllib.request
import uuid
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
import psycopg
import psycopg.errors

from operations_kpi_logging import configure_logging, log_db_url_safe
from operations_kpi_data import (
    OpsKpiTargets,
    build_periods,
    load_daily_availability_from_database,
    load_dashboard_payload,
    load_ops_kpi_targets,
    ops_kpi_data_fingerprint,
)
from operations_kpi_insights import (
    METRICS,
    ROW_KINDS,
    build_cell_insight_csv,
    compute_cell_insight,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_TEMPLATE = ROOT / "Operations KPI.html"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8054
DATA_PLACEHOLDER = "__DASHBOARD_DATA__"
BASIC_AUTH_REALM = "Operations KPI Dashboard"

_analysis_cache: dict[str, tuple[str, object, dict, OpsKpiTargets]] = {}

_dashboard_warm = threading.Event()
_last_render_fingerprint: dict[tuple[str, float], str] = {}
_fingerprint_cache: tuple[float, str] | None = None
_fingerprint_cache_lock = threading.Lock()

_dashboard_logger = logging.getLogger("operations_kpi.dashboard")

_LOADING_PAGE_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>Operations KPI — loading</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
body{font-family:system-ui,sans-serif;max-width:36rem;margin:3rem auto;line-height:1.5;color:#0f172a}
.spinner{display:inline-block;width:1.25rem;height:1.25rem;border:2px solid #cbd5e1;
border-top-color:#2563eb;border-radius:50%;animation:spin .8s linear infinite;vertical-align:middle;margin-right:.5rem}
@keyframes spin{to{transform:rotate(360deg)}}
</style></head>
<body>
<p><span class="spinner" aria-hidden="true"></span>Loading Operations KPI dashboard…</p>
<p class="text-slate-600" style="color:#64748b;font-size:.9rem">
First load can take several minutes while data is prepared from PostgreSQL.
This page will refresh automatically when ready.
</p>
<script>
(function poll(){
  fetch('/readyz',{cache:'no-store'}).then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
    .then(function(x){
      if(x.ok&&x.j&&x.j.status==='ready'){location.reload();return;}
      setTimeout(poll,2000);
    })
    .catch(function(){setTimeout(poll,3000);});
})();
</script>
</body></html>"""

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
_request_path: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_path", default=None
)


class _RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        rid = _request_id.get()
        path = _request_path.get()
        if rid:
            prefix = f"[req={rid}"
            if path:
                prefix += f" path={path}"
            prefix += "] "
            record.msg = prefix + str(record.msg)
        return True


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


def mark_dashboard_warm() -> None:
    _dashboard_warm.set()


def is_dashboard_warm() -> bool:
    return _dashboard_warm.is_set()


def reset_dashboard_warm_state() -> None:
    """Clear warm/ fingerprint hints (tests)."""
    _dashboard_warm.clear()
    _last_render_fingerprint.clear()
    global _fingerprint_cache
    with _fingerprint_cache_lock:
        _fingerprint_cache = None


def _readyz_require_warm() -> bool:
    return _env_flag(
        "OPERATIONS_KPI_READYZ_REQUIRE_WARM",
        default=_env_flag("OPERATIONS_KPI_PREWARM", default=True),
    )


def _serve_loading_until_warm() -> bool:
    return _env_flag("OPERATIONS_KPI_SERVE_LOADING_UNTIL_WARM", default=True)


def _fingerprint_cache_seconds() -> float:
    return float(os.environ.get("OPERATIONS_KPI_FINGERPRINT_CACHE_SECONDS", "60"))


def _cached_ops_kpi_data_fingerprint(database_url: str) -> str:
    """Return data fingerprint; reuse recent value when render cache is warm."""
    global _fingerprint_cache
    ttl = _fingerprint_cache_seconds()
    if ttl > 0 and is_dashboard_warm():
        with _fingerprint_cache_lock:
            if _fingerprint_cache is not None:
                cached_at, cached_fp = _fingerprint_cache
                if time.monotonic() - cached_at < ttl:
                    return cached_fp
    fp = ops_kpi_data_fingerprint(database_url)
    if ttl > 0:
        with _fingerprint_cache_lock:
            _fingerprint_cache = (time.monotonic(), fp)
    return fp


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


def _basic_auth_username() -> str | None:
    value = os.environ.get("OPERATIONS_KPI_BASIC_AUTH_USERNAME", "")
    return value or None


def _basic_auth_password() -> str | None:
    value = os.environ.get("OPERATIONS_KPI_BASIC_AUTH_PASSWORD", "")
    return value or None


def _default_listen_port() -> int:
    """CapRover and similar platforms set PORT; prefer it over OPERATIONS_KPI_PORT."""
    if os.environ.get("PORT") is not None:
        return _env_int("PORT", DEFAULT_PORT)
    return _env_int("OPERATIONS_KPI_PORT", DEFAULT_PORT)


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
            _dashboard_logger.debug("analysis context cache hit fingerprint=%s", fp)
            return entry[1], entry[2], entry[3]
    _dashboard_logger.info(
        "analysis context cache miss; loading from database fingerprint=%s",
        fp,
    )
    t0 = time.perf_counter()
    df = load_daily_availability_from_database(database_url)
    periods = build_periods(df)
    tgt_url = targets_database_url if targets_database_url is not None else database_url
    targets = load_ops_kpi_targets(tgt_url)
    with _analysis_lock:
        _analysis_cache[key] = (fp, df, periods, targets)
    _dashboard_logger.info(
        "analysis context loaded in %.2fs rows=%d",
        time.perf_counter() - t0,
        len(df),
    )
    return df, periods, targets


def parse_args() -> argparse.Namespace:
    # Repo .env then CWD .env so DATABASE_URL resolves whether you launch from ~/ops_kpi or elsewhere.
    load_dotenv(ROOT / ".env")
    load_dotenv()
    env_default_host = os.environ.get("OPERATIONS_KPI_HOST", DEFAULT_HOST)
    env_default_port = _default_listen_port()
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
    require_warm_for_readyz: bool | None = None,
    serve_loading_until_warm: bool | None = None,
    basic_auth_username: str | None = None,
    basic_auth_password: str | None = None,
):
    if require_warm_for_readyz is None:
        require_warm_for_readyz = _readyz_require_warm()
    if serve_loading_until_warm is None:
        serve_loading_until_warm = _serve_loading_until_warm()
    if basic_auth_username is None:
        basic_auth_username = _basic_auth_username()
    if basic_auth_password is None:
        basic_auth_password = _basic_auth_password()
    auth_enabled = bool(basic_auth_username and basic_auth_password)
    if not auth_enabled:
        logger.warning(
            "HTTP Basic Auth is disabled; the dashboard and its APIs are open to "
            "anyone who can reach this host. Set OPERATIONS_KPI_BASIC_AUTH_USERNAME "
            "and OPERATIONS_KPI_BASIC_AUTH_PASSWORD to require a login."
        )

    @lru_cache(maxsize=32)
    def _rendered_html_cached(
        data_fp: str, template_path_str: str, template_mtime: float
    ) -> str:
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

    def rendered_html(data_fp: str, template_path_str: str, template_mtime: float) -> str:
        info_before = _rendered_html_cached.cache_info()
        html_out = _rendered_html_cached(data_fp, template_path_str, template_mtime)
        info_after = _rendered_html_cached.cache_info()
        if info_after.hits > info_before.hits:
            logger.debug("rendered_html cache hit fingerprint=%s", data_fp)
        elif info_after.misses > info_before.misses:
            logger.debug("rendered_html cache miss fingerprint=%s", data_fp)
            mark_dashboard_warm()
        return html_out

    def resolve_dashboard_html() -> str:
        path_str = str(template_path.resolve())
        mtime = template_path.stat().st_mtime
        cache_key = (path_str, mtime)
        stored_fp = _last_render_fingerprint.get(cache_key)
        if stored_fp is not None:
            info_before = _rendered_html_cached.cache_info()
            html_out = _rendered_html_cached(stored_fp, path_str, mtime)
            if _rendered_html_cached.cache_info().hits > info_before.hits:
                current_fp = _cached_ops_kpi_data_fingerprint(database_url)
                if current_fp == stored_fp:
                    logger.debug(
                        "dashboard render cache hit; skipped full fingerprint round-trip"
                    )
                    return html_out
                logger.info(
                    "dashboard data changed (fingerprint %s -> %s); re-rendering",
                    stored_fp[:48],
                    current_fp[:48],
                )
                with _fingerprint_cache_lock:
                    global _fingerprint_cache
                    _fingerprint_cache = None
        data_fp = _cached_ops_kpi_data_fingerprint(database_url)
        html_out = rendered_html(data_fp, path_str, mtime)
        _last_render_fingerprint[cache_key] = data_fp
        mark_dashboard_warm()
        return html_out

    class DashboardHandler(BaseHTTPRequestHandler):
        def _bind_request_context(self) -> tuple[contextvars.Token, contextvars.Token]:
            rid = uuid.uuid4().hex[:8]
            path = urlparse(self.path).path
            return _request_id.set(rid), _request_path.set(path)

        def _clear_request_context(
            self,
            token_id: contextvars.Token,
            token_path: contextvars.Token,
        ) -> None:
            _request_id.reset(token_id)
            _request_path.reset(token_path)

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

        def _has_valid_basic_auth(self) -> bool:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                decoded = base64.b64decode(
                    header[len("Basic "):], validate=True
                ).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                return False
            user, sep, password = decoded.partition(":")
            if not sep:
                return False
            return secrets.compare_digest(
                user, basic_auth_username
            ) and secrets.compare_digest(password, basic_auth_password)

        def _send_auth_required(self, *, send_body: bool) -> None:
            body = b"Authentication required."
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header(
                "WWW-Authenticate", f'Basic realm="{BASIC_AUTH_REALM}", charset="UTF-8"'
            )
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def _require_basic_auth(self, *, send_body: bool) -> bool:
            """Gate access to dashboard content/APIs; health/readiness probes bypass this."""
            if not auth_enabled or self._has_valid_basic_auth():
                return True
            logger.info(
                "Rejected unauthenticated request to %s from %s",
                self.path,
                self.address_string(),
            )
            self._send_auth_required(send_body=send_body)
            return False

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
            except Exception as exc:  # pragma: no cover - network/database dependent
                logger.warning(
                    "Readiness check failed (%s): database=%s",
                    type(exc).__name__,
                    log_db_url_safe(database_url),
                    exc_info=True,
                )
                payload = {
                    "status": "not_ready",
                    "error": self._error_message(exc, fallback="Database unavailable"),
                }
                self._send_json(
                    payload,
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                    send_body=send_body,
                )
                return
            if require_warm_for_readyz and not is_dashboard_warm():
                self._send_json(
                    {
                        "status": "not_ready",
                        "reason": "dashboard_cache_warming",
                    },
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                    send_body=send_body,
                )
                return
            self._send_json({"status": "ready"}, send_body=send_body)

        def _is_internal_full_render(self) -> bool:
            """Loopback or explicit pre-warm header may block on full dashboard render."""
            if self.headers.get("X-Operations-Kpi-External-Client", "").strip() == "1":
                return False
            if self.headers.get("X-Operations-Kpi-Prewarm", "").strip() == "1":
                return True
            host = self.client_address[0] if self.client_address else ""
            return host in {"127.0.0.1", "::1"}

        def _serve_loading_page(self, send_body: bool) -> None:
            body = _LOADING_PAGE_HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def _parse_cell_insight_query(
            self,
        ) -> tuple[str, str, str, str, str | None]:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query, keep_blank_values=True)

            def first(key: str) -> str | None:
                vals = qs.get(key)
                if not vals:
                    return None
                return vals[0] if vals[0] != "" else None

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
            return row_kind, region, metric, period, zoo

        def _send_csv(self, body: bytes, filename: str) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{filename}"',
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_cell_insight(self) -> None:
            try:
                row_kind, region, metric, period, zoo = self._parse_cell_insight_query()
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
                logger.warning("cell insight bad request: %s", exc)
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

        def _serve_cell_insight_export(self) -> None:
            try:
                row_kind, region, metric, period, zoo = self._parse_cell_insight_query()
                df, periods, _ops_targets = get_analysis_context(
                    database_url=database_url,
                    targets_database_url=targets_database_url,
                )
                body, filename = build_cell_insight_csv(
                    df,
                    periods,
                    row_kind,
                    region,
                    zoo,
                    metric,
                    period,
                )
                self._send_csv(body, filename)
            except ValueError as exc:
                logger.warning("cell insight export bad request: %s", exc)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as exc:  # pragma: no cover
                logger.exception("Unexpected cell insight export failure")
                self._send_json(
                    {
                        "error": self._error_message(
                            exc, fallback="Internal server error"
                        )
                    },
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )

        def _serve_dashboard_payload_json(self) -> None:
            try:
                if require_warm_for_readyz and not is_dashboard_warm():
                    self._send_json(
                        {
                            "status": "not_ready",
                            "reason": "dashboard_cache_warming",
                        },
                        status=HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                    return
                payload = load_dashboard_payload(
                    database_url,
                    targets_database_url=targets_database_url,
                )
                self._send_json({"status": "ok", "data": payload})
            except psycopg.Error as exc:  # pragma: no cover
                self._send_json(
                    {
                        "status": "error",
                        "error": self._error_message(
                            exc, fallback="Database error"
                        ),
                    },
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
            except ValueError as exc:
                self._send_json(
                    {"status": "error", "error": str(exc)},
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
            except Exception as exc:  # pragma: no cover
                logger.exception("Unexpected dashboard payload failure")
                self._send_json(
                    {
                        "status": "error",
                        "error": self._error_message(
                            exc, fallback="Internal server error"
                        ),
                    },
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )

        def _serve_dashboard(self, send_body: bool) -> None:
            if (
                serve_loading_until_warm
                and not is_dashboard_warm()
                and not self._is_internal_full_render()
            ):
                logger.info(
                    "Serving loading page (dashboard cache not warm yet; avoids proxy 504)"
                )
                self._serve_loading_page(send_body)
                return
            t0 = time.perf_counter()
            try:
                html = resolve_dashboard_html()
            except psycopg.Error as exc:  # pragma: no cover - database dependent
                if isinstance(exc, psycopg.errors.InsufficientPrivilege):
                    kind = "permission"
                elif isinstance(exc, psycopg.OperationalError):
                    kind = "connect"
                else:
                    kind = "query"
                logger.error(
                    "Dashboard unavailable: database error (kind=%s)",
                    kind,
                    exc_info=True,
                )
                detail = self._error_message(exc, fallback="")
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
                logger.warning("Dashboard unavailable: data shape error: %s", exc)
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
            logger.info(
                "Dashboard rendered in %.2fs (%d bytes)",
                time.perf_counter() - t0,
                len(body),
            )

        def _handle_get(self, *, send_body: bool) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                self._serve_healthz(send_body=send_body)
                return
            if parsed.path == "/readyz":
                self._serve_readyz(send_body=send_body)
                return
            if not self._require_basic_auth(send_body=send_body):
                return
            if parsed.path == "/api/cell-insight":
                self._serve_cell_insight()
                return
            if parsed.path == "/api/cell-insight/export":
                self._serve_cell_insight_export()
                return
            if parsed.path == "/api/dashboard-payload":
                self._serve_dashboard_payload_json()
                return
            if parsed.path not in {"/", "/index.html"}:
                self.send_error(HTTPStatus.NOT_FOUND, "Page not found")
                return
            self._serve_dashboard(send_body=send_body)

        def do_GET(self) -> None:
            token_id, token_path = self._bind_request_context()
            t0 = time.perf_counter()
            try:
                self._handle_get(send_body=True)
            finally:
                logger.debug(
                    "GET %s completed in %.2fs",
                    self.path,
                    time.perf_counter() - t0,
                )
                self._clear_request_context(token_id, token_path)

        def do_HEAD(self) -> None:
            token_id, token_path = self._bind_request_context()
            t0 = time.perf_counter()
            try:
                self._handle_get(send_body=False)
            finally:
                logger.debug(
                    "HEAD %s completed in %.2fs",
                    self.path,
                    time.perf_counter() - t0,
                )
                self._clear_request_context(token_id, token_path)

        def log_message(self, format: str, *args) -> None:
            logger.info("%s - %s", self.address_string(), format % args)

    return DashboardHandler


def _prewarm_dashboard_cache(
    *,
    logger: logging.Logger,
    port: int,
    basic_auth_username: str | None = None,
    basic_auth_password: str | None = None,
) -> None:
    """Request / on loopback so the heavy DB render runs once and lru_cache stays hot."""
    time.sleep(0.35)
    deadline_s = float(os.environ.get("OPERATIONS_KPI_PREWARM_TIMEOUT", "900"))
    url = f"http://127.0.0.1:{port}/"
    t0 = time.perf_counter()
    headers = {"X-Operations-Kpi-Prewarm": "1"}
    if basic_auth_username and basic_auth_password:
        token = base64.b64encode(
            f"{basic_auth_username}:{basic_auth_password}".encode("utf-8")
        ).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=deadline_s) as resp:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
        mark_dashboard_warm()
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
    configure_logging(args.log_level)
    logger = logging.getLogger("operations_kpi.dashboard")
    if not any(isinstance(f, _RequestContextFilter) for f in logger.filters):
        logger.addFilter(_RequestContextFilter())
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

    basic_auth_username = _basic_auth_username()
    basic_auth_password = _basic_auth_password()

    handler = make_handler(
        template_path,
        database_url=database_url,
        targets_database_url=targets_database_url,
        debug_errors=args.debug_errors,
        logger=logger,
        basic_auth_username=basic_auth_username,
        basic_auth_password=basic_auth_password,
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
    logger.info("Data source: PostgreSQL (%s)", log_db_url_safe(database_url))
    logger.info("HTML template: %s", template_path)
    if _readyz_require_warm():
        logger.info(
            "Readiness /readyz requires DB plus warm dashboard cache "
            "(set OPERATIONS_KPI_READYZ_REQUIRE_WARM=false to only ping DB)."
        )
    if _serve_loading_until_warm():
        logger.info(
            "Cold GET / returns a fast loading page until the cache is warm "
            "(set OPERATIONS_KPI_SERVE_LOADING_UNTIL_WARM=false to block on full render)."
        )
    logger.info(
        "Proxy: increase proxy_read_timeout (see deploy/nginx-proxy-timeouts.conf); "
        "use /healthz for liveness, /readyz for traffic."
    )
    if basic_auth_username and basic_auth_password:
        logger.info(
            "HTTP Basic Auth is required for the dashboard and its APIs "
            "(/healthz and /readyz remain open for health checks)."
        )
    if args.prewarm:
        logger.info(
            "Pre-warming dashboard in the background (avoids a multi-minute blank wait on first / in the browser).",
        )
        threading.Thread(
            target=_prewarm_dashboard_cache,
            kwargs={
                "logger": logger,
                "port": args.port,
                "basic_auth_username": basic_auth_username,
                "basic_auth_password": basic_auth_password,
            },
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
