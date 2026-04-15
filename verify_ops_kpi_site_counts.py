#!/usr/bin/env python3
"""Compare Site-first counts: raw Site table vs dashboard-prepared TOTAL."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

from operations_kpi_data import count_unique_sites, load_daily_availability_from_database, scope_frame

ROOT = Path(__file__).resolve().parent

SQL_RAW = "SELECT COUNT(DISTINCT site_id)::bigint AS n FROM site"

SQL_FILTERED = """
SELECT COUNT(DISTINCT site_id)::bigint AS n
FROM site
WHERE UPPER(BTRIM(COALESCE(region, ''))) IN ('NCR', 'NLZ', 'SLZ', 'VIS', 'MIN')
"""


def main() -> None:
    load_dotenv(ROOT / ".env")
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL not set (.env or environment).", file=sys.stderr)
        raise SystemExit(1)

    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(SQL_RAW)
            raw_n = cur.fetchone()[0]
            cur.execute(SQL_FILTERED)
            filt_n = cur.fetchone()[0]

    df = load_daily_availability_from_database(url)
    prepared_n = count_unique_sites(scope_frame(df, "Overall"))

    print(
        "COUNT(DISTINCT site.site_id) — full Site table:",
        raw_n,
    )
    print(
        "COUNT(DISTINCT site.site_id) — Site rows in dashboard regions (NCR,NLZ,SLZ,VIS,MIN):",
        filt_n,
    )
    print("Python TOTAL row — prepared frame + site_table_site_id (dashboard):", prepared_n)
    print(
        "\nNote: Dashboard TOTAL should align with SQL_FILTERED for Site-first loading. "
        "Any gap usually indicates region normalization differences."
    )


if __name__ == "__main__":
    main()
