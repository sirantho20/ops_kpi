"""Shared logging helpers for Operations KPI (dashboard, data, insights, ETL CLIs).

Environment:
  OPERATIONS_KPI_LOG_LEVEL — default log level (INFO, DEBUG, WARNING, ERROR).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse


_DEFAULT_LEVEL = "INFO"
_ENV_LOG_LEVEL = "OPERATIONS_KPI_LOG_LEVEL"

_INFO_FORMAT = "%(asctime)s %(levelname)s %(name)s - %(message)s"
_DEBUG_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s %(filename)s:%(lineno)d - %(message)s"
)


def resolve_log_level(level: str | None = None) -> int:
    """Map a level name to a logging constant; unknown names fall back to INFO."""
    name = (level or os.environ.get(_ENV_LOG_LEVEL) or _DEFAULT_LEVEL).upper()
    return getattr(logging, name, logging.INFO)


def configure_logging(level: str | None = None) -> None:
    """Configure root logging once (idempotent). DEBUG adds file:line to the format."""
    numeric = resolve_log_level(level)
    fmt = _DEBUG_FORMAT if numeric <= logging.DEBUG else _INFO_FORMAT
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=numeric, format=fmt)
    else:
        root.setLevel(numeric)
        for handler in root.handlers:
            handler.setLevel(numeric)
            handler.setFormatter(logging.Formatter(fmt))


def add_log_level_arg(parser: Any, *, default: str | None = None) -> None:
    """Add --log-level to an argparse parser (default from env or INFO)."""
    parser.add_argument(
        "--log-level",
        default=default or os.environ.get(_ENV_LOG_LEVEL, _DEFAULT_LEVEL),
        help="Python logging level (DEBUG, INFO, WARNING, ERROR).",
    )


def log_db_url_safe(url: str | None) -> str:
    """Return host/database fragment of a DB URL without credentials."""
    if not url:
        return "<unset>"
    try:
        parsed = urlparse(url)
    except Exception:
        return "<invalid-url>"
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    db = (parsed.path or "").lstrip("/") or ""
    return f"{host}{port}/{db}" if db else f"{host}{port}" or "<empty>"


@contextmanager
def log_timing(
    logger: logging.Logger,
    label: str,
    **context: Any,
) -> Iterator[None]:
    """Log start at DEBUG and completion at INFO with elapsed seconds and optional context."""
    ctx_suffix = ""
    if context:
        parts = [f"{k}={v!r}" for k, v in context.items()]
        ctx_suffix = " (" + ", ".join(parts) + ")"
    logger.debug("%s started%s", label, ctx_suffix)
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - t0
        logger.info("%s finished in %.2fs%s", label, elapsed, ctx_suffix)
