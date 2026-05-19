#!/usr/bin/env python3
"""Compare Site-first counts: raw Site table vs dashboard-prepared TOTAL."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

from operations_kpi_data import count_unique_sites, load_daily_availability_from_database, scope_frame
from operations_kpi_logging import add_log_level_arg, configure_logging, log_db_url_safe

ROOT = Path(__file__).resolve().parent
logger = logging.getLogger("operations_kpi.etl.verify_ops_kpi_site_counts")

SQL_RAW = "SELECT COUNT(DISTINCT site_id)::bigint AS n FROM site"

SQL_FILTERED = """
SELECT COUNT(DISTINCT site_id)::bigint AS n
FROM site
WHERE UPPER(BTRIM(COALESCE(region, ''))) IN ('NCR', 'NLZ', 'SLZ', 'VIS', 'MIN')
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
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(SQL_RAW)
            raw_n = cur.fetchone()[0]
            cur.execute(SQL_FILTERED)
            filt_n = cur.fetchone()[0]

    df = load_daily_availability_from_database(url)
    prepared_n = count_unique_sites(scope_frame(df, "Overall"))

    logger.info(
        "COUNT(DISTINCT site.site_id) — full Site table: %s",
        raw_n,
    )
    logger.info(
        "COUNT(DISTINCT site.site_id) — Site rows in dashboard regions (NCR,NLZ,SLZ,VIS,MIN): %s",
        filt_n,
    )
    logger.info(
        "Python TOTAL row — prepared frame + site_table_site_id (dashboard): %s",
        prepared_n,
    )
    if prepared_n != filt_n:
        logger.warning(
            "Site count mismatch: dashboard TOTAL=%s vs SQL filtered=%s (delta=%s)",
            prepared_n,
            filt_n,
            prepared_n - filt_n,
        )
    logger.info(
        "Note: Dashboard TOTAL should align with SQL_FILTERED for Site-first loading. "
        "Any gap usually indicates region normalization differences."
    )


if __name__ == "__main__":
    main()
