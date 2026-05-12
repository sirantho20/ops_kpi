#!/usr/bin/env python3
"""Create ops_kpi_* tables (optional) and load from daily_availability_transformed.csv."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pandas as pd
import psycopg
from dotenv import load_dotenv

from operations_kpi_data import prepare_daily_availability_dataframe

ROOT = Path(__file__).resolve().parent
DEFAULT_CSV = ROOT / "daily_availability_transformed.csv"
SCHEMA_PATH = ROOT / "ops_kpi_schema.sql"

INSERT_AVAILABILITY = """
INSERT INTO ops_kpi_availability (
    site_id, date, pla_id, ptci_number, region, zoo, territory,
    incident_count, outage_mins, accepted_outage_minutes, availability,
    uptime_per_tenant, total_available_minutes
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
"""

INSERT_SIC = """
INSERT INTO ops_kpi_sic (site_id, date, sic_count) VALUES (%s, %s, %s)
"""

INSERT_CM = """
INSERT INTO ops_kpi_cm (site_id, date, cm_count) VALUES (%s, %s, %s)
"""

# Map KPI keys (PLA when present, else site PK) to canonical site.site_id; merge duplicate keys.
_NORMALIZE_SIC_UPDATE = """
UPDATE ops_kpi_sic v
SET site_id = s.site_id::text
FROM site s
WHERE v.site_id::text = COALESCE(NULLIF(BTRIM(s.pla_id::text), ''), s.site_id::text)
  AND v.site_id::text IS DISTINCT FROM s.site_id::text
"""

_NORMALIZE_SIC_VIA_AVAIL_PTCI = """
UPDATE ops_kpi_sic v
SET site_id = s.site_id::text
FROM ops_kpi_availability a
INNER JOIN site s ON s.site_id = NULLIF(BTRIM(a.ptci_number::text), '')
WHERE v.site_id = a.site_id
  AND v.date = a.date
  AND v.site_id::text IS DISTINCT FROM s.site_id::text
"""

_BATCH = 10_000


def _optional_str(v: object) -> str | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    if s in ("", "nan", "None", "<NA>"):
        return None
    return s


def _optional_float(v: object) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _series_optional_str(s: pd.Series) -> list[str | None]:
    out: list[str | None] = []
    for v in s.tolist():
        out.append(_optional_str(v))
    return out


def _series_optional_float(s: pd.Series) -> list[float | None]:
    out: list[float | None] = []
    for v in s.tolist():
        out.append(_optional_float(v))
    return out


def build_rows_simple(df: pd.DataFrame) -> tuple[list, list, list]:
    """Build insert rows (vectorized for large CSVs)."""
    site_ids = df["site_key"].astype(str)
    days = pd.to_datetime(df["Date"], errors="coerce").dt.date
    terr = df["Teritory"] if "Teritory" in df.columns else pd.Series([""] * len(df), index=df.index)
    terr = terr.fillna("").astype(str).str.strip().replace({"nan": ""})

    avail = list(
        zip(
            site_ids.tolist(),
            days.tolist(),
            _series_optional_str(df["PLA ID"]),
            _series_optional_str(df["PTCI Number"]),
            df["Region"].astype(str).tolist(),
            df["Zoo"].astype(str).tolist(),
            terr.tolist(),
            _series_optional_float(df["Incident_count"]),
            _series_optional_float(df["Outage_mins"]),
            _series_optional_float(df["Accepted Outage Minutes"]),
            _series_optional_float(df["Availability"]),
            _series_optional_float(df["Uptime_per_tenant"]),
            _series_optional_float(df["Total Available Minutes"]),
        )
    )
    sic = pd.to_numeric(df["SIC Count"], errors="coerce").fillna(0).astype(int)
    cc = pd.to_numeric(df["CM Count"], errors="coerce").fillna(0).astype(int)
    sics = list(zip(site_ids.tolist(), days.tolist(), sic.tolist()))
    cms = list(zip(site_ids.tolist(), days.tolist(), cc.tolist()))
    return avail, sics, cms


def apply_schema(conn: psycopg.Connection) -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def load_prepared_csv(csv_path: Path) -> pd.DataFrame:
    raw = pd.read_csv(csv_path, low_memory=False)
    return prepare_daily_availability_dataframe(raw, territory_source="excel")


def _executemany_batches(cur, sql: str, rows: list[tuple]) -> None:
    for i in range(0, len(rows), _BATCH):
        cur.executemany(sql, rows[i : i + _BATCH])


def _normalize_ops_kpi_sic(cur: psycopg.Cursor) -> None:
    """Align SIC site_id with public.site.site_id; collapse duplicate PKs with summed counts."""
    cur.execute(_NORMALIZE_SIC_UPDATE)
    cur.execute(_NORMALIZE_SIC_VIA_AVAIL_PTCI)
    cur.execute(
        """
        CREATE TEMP TABLE _ops_kpi_sic_merged AS
        SELECT site_id, date, SUM(sic_count)::integer AS sic_count
        FROM ops_kpi_sic
        GROUP BY site_id, date
        """
    )
    cur.execute("TRUNCATE ops_kpi_sic")
    cur.execute(
        """
        INSERT INTO ops_kpi_sic (site_id, date, sic_count)
        SELECT site_id, date, sic_count FROM _ops_kpi_sic_merged
        """
    )
    cur.execute("DROP TABLE _ops_kpi_sic_merged")


def migrate(conn: psycopg.Connection, df: pd.DataFrame) -> None:
    avail, sics, cms = build_rows_simple(df)
    with conn.cursor() as cur:
        # No FK from SIC/CM into availability; truncate all fact tables explicitly.
        cur.execute("TRUNCATE ops_kpi_availability, ops_kpi_sic, ops_kpi_cm")
        _executemany_batches(cur, INSERT_AVAILABILITY, avail)
        _executemany_batches(cur, INSERT_SIC, sics)
        _normalize_ops_kpi_sic(cur)
        _executemany_batches(cur, INSERT_CM, cms)
    conn.commit()
    print(f"Inserted {len(avail)} availability, {len(sics)} SIC, {len(cms)} cm rows.")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV,
        help="Transformed availability CSV (default: daily_availability_transformed.csv).",
    )
    p.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="PostgreSQL URL (default: DATABASE_URL). Not required with --dry-run.",
    )
    p.add_argument(
        "--apply-schema",
        action="store_true",
        help=f"Run {SCHEMA_PATH.name} before loading.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate CSV and print row counts only.",
    )
    return p.parse_args()


def main() -> None:
    load_dotenv(ROOT / ".env")
    args = parse_args()
    csv_path = Path(args.csv)
    if not csv_path.is_absolute():
        csv_path = (ROOT / csv_path).resolve()
    else:
        csv_path = csv_path.resolve()
    if not csv_path.is_file():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    df = load_prepared_csv(csv_path)
    print(f"Prepared {len(df)} rows from {csv_path}")

    if args.dry_run:
        avail, sics, cms = build_rows_simple(df)
        print(
            f"dry-run: {len(avail)} availability, {len(sics)} SIC, {len(cms)} cm rows"
        )
        return

    if not args.database_url:
        raise SystemExit("DATABASE_URL or --database-url is required (unless --dry-run).")

    with psycopg.connect(args.database_url) as conn:
        if args.apply_schema:
            apply_schema(conn)
        migrate(conn, df)


if __name__ == "__main__":
    main()
