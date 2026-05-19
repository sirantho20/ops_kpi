#!/usr/bin/env python3
"""Compare monthly CM: PostgreSQL (ops_kpi_cm + site, dashboard regions) vs Python dashboard path."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import pandas as pd
import psycopg
from dotenv import load_dotenv

from operations_kpi_data import (
    _chart_month_axes,
    _pg_column_exists,
    load_daily_availability_from_database,
    monthly_cm_count,
    scope_frame,
)
from operations_kpi_logging import add_log_level_arg, configure_logging, log_db_url_safe

ROOT = Path(__file__).resolve().parent
logger = logging.getLogger("operations_kpi.etl.verify_ops_kpi_cm_counts")

_SQL_MONTHLY_CM_SUM = """
SELECT
    DATE_TRUNC('month', cm.event_date)::date AS month_start,
    COALESCE(SUM(cm.cm_count), 0)::bigint AS total
FROM ops_kpi_cm cm
INNER JOIN site t ON cm.site_id = t.site_id
WHERE UPPER(BTRIM(COALESCE(t.region, ''))) IN ('NCR', 'NLZ', 'SLZ', 'VIS', 'MIN')
GROUP BY DATE_TRUNC('month', cm.event_date)
ORDER BY DATE_TRUNC('month', cm.event_date);
"""

_SQL_MONTHLY_CM_COUNT = """
SELECT
    DATE_TRUNC('month', cm.event_date)::date AS month_start,
    COUNT(*)::bigint AS total
FROM ops_kpi_cm cm
INNER JOIN site t ON cm.site_id = t.site_id
WHERE UPPER(BTRIM(COALESCE(t.region, ''))) IN ('NCR', 'NLZ', 'SLZ', 'VIS', 'MIN')
GROUP BY DATE_TRUNC('month', cm.event_date)
ORDER BY DATE_TRUNC('month', cm.event_date);
"""


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    add_log_level_arg(parser)
    args = parser.parse_args()
    configure_logging(args.log_level)

    url = os.environ.get("DATABASE_URL")
    if not url:
        logger.error("DATABASE_URL not set (.env or environment).")
        raise SystemExit(1)

    logger.info("Connecting to %s", log_db_url_safe(url))
    has_cm_col = False
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            has_cm_col = _pg_column_exists(cur, "ops_kpi_cm", "cm_count")
            cur.execute(_SQL_MONTHLY_CM_SUM if has_cm_col else _SQL_MONTHLY_CM_COUNT)
            sql_rows = cur.fetchall()

    sql_by_period: dict[pd.Period, int] = {}
    for month_start, total in sql_rows:
        if month_start is None:
            continue
        p = pd.Timestamp(month_start).to_period("M")
        sql_by_period[p] = int(total)

    df = load_daily_availability_from_database(url)
    month_periods, month_labels = _chart_month_axes(df)
    dash_values = monthly_cm_count(scope_frame(df, "Overall"), month_periods)

    logger.info("month (chart axis)\tpython_dashboard_cm\tsql_reference_total\tmatch")
    mismatches = 0
    for label, period, py_val in zip(month_labels, month_periods, dash_values):
        sql_val = sql_by_period.get(period)
        py_n = py_val if py_val is not None else None
        sql_n = sql_val
        if py_n is None and sql_n is None:
            ok = True
        elif py_n is None or sql_n is None:
            ok = False
        else:
            ok = py_n == sql_n
        if not ok:
            mismatches += 1
            logger.warning(
                "CM count mismatch for %s: python=%s sql=%s",
                label,
                py_n,
                sql_n,
            )
        match_s = "yes" if ok else "no"
        logger.info("%s\t%s\t%s\t%s", label, py_n, sql_n, match_s)

    sql_mode = "SUM(cm.cm_count)" if has_cm_col else "COUNT(*)"
    logger.info(
        "SQL uses ops_kpi_cm + site only (dashboard regions), %s by calendar month. "
        "Python uses load_daily_availability_from_database + monthly_cm_count (Overall), "
        "same chart month axis as the dashboard.",
        sql_mode,
    )
    if mismatches:
        logger.warning("Found %d month(s) with CM count mismatch", mismatches)


if __name__ == "__main__":
    main()
