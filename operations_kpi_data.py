from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import pandas as pd
import psycopg

from operations_kpi_logging import log_timing

logger = logging.getLogger("operations_kpi.data")


@dataclass(frozen=True)
class OpsKpiTargets:
    """Per-metric baseline factors (prev-FY total × factor) plus fixed MTTR and availability."""

    events_baseline_factor: float
    cm_baseline_factor: float
    visit_baseline_factor: float
    mttr_minutes: float
    availability_pct: float
    availability_pct_ncr: float


def default_ops_kpi_targets() -> OpsKpiTargets:
    return OpsKpiTargets(
        events_baseline_factor=0.85,
        cm_baseline_factor=0.85,
        visit_baseline_factor=0.85,
        mttr_minutes=200.0,
        availability_pct=99.96,
        availability_pct_ncr=99.98,
    )


def load_ops_kpi_targets(database_url: str | None) -> OpsKpiTargets:
    d = default_ops_kpi_targets()
    if not database_url:
        return d
    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT metric_key, value FROM ops_kpi_targets")
                rows = {str(k): float(v) for k, v in cur.fetchall()}
    except Exception:
        logger.warning(
            "Using default targets; could not load ops_kpi_targets",
            exc_info=True,
        )
        return d
    legacy = rows.get("baseline_factor")
    ev = rows.get("events_baseline_factor", legacy if legacy is not None else d.events_baseline_factor)
    cm = rows.get("cm_baseline_factor", legacy if legacy is not None else d.cm_baseline_factor)
    vis = rows.get("visit_baseline_factor", legacy if legacy is not None else d.visit_baseline_factor)
    mt = rows.get("mttr_minutes", d.mttr_minutes)
    ap = rows.get("availability_pct", d.availability_pct)
    ap_ncr = rows.get("availability_pct_ncr", d.availability_pct_ncr)
    loaded = OpsKpiTargets(
        events_baseline_factor=float(ev),
        cm_baseline_factor=float(cm),
        visit_baseline_factor=float(vis),
        mttr_minutes=float(mt),
        availability_pct=float(ap),
        availability_pct_ncr=float(ap_ncr),
    )
    logger.info("Loaded ops_kpi_targets (%d metric keys from database)", len(rows))
    return loaded


def availability_pct_for_region_scope(region: str, targets: OpsKpiTargets) -> float:
    """Availability TARGET for a dashboard region row or chart scope (Overall, NCR, …)."""
    if region == "NCR":
        return targets.availability_pct_ncr
    return targets.availability_pct


def _pg_column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
          AND column_name = %s
        LIMIT 1
        """,
        (table, column),
    )
    return cur.fetchone() is not None


def _pg_table_exists(cur, table: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = %s
        LIMIT 1
        """,
        (table,),
    )
    return cur.fetchone() is not None


@dataclass(frozen=True)
class OpsKpiSicColumns:
    site_column: str
    site_join_dimension: Literal["site_table_site_id", "kpi_site_id"]
    date_column: str
    value_column: str | None
    value_mode: Literal["sum", "distinct_count", "row_count"] = "sum"


# Site visit facts: separate from SIC. Table discovery order (public schema).
SITE_VISIT_TABLE_CANDIDATES: tuple[str, ...] = (
    "ops_kpi_site_visit",
    "ops_kpi_sitevisit",
    "site_visit",
    "site_visits",
)

# Table columns shown for metrics ``visit`` (SIC) and ``siteVisit`` only.
VISIT_TABLE_FY_YEARS: tuple[int, int] = (2025, 2026)
VISIT_TABLE_TOTAL_PERIOD_KEY = "TOTAL"
SITE_VISIT_TABLE_MONTH_PERIODS: tuple[pd.Period, ...] = tuple(
    pd.Period(f"{VISIT_TABLE_FY_YEARS[1]}-{month:02d}", freq="M") for month in range(1, 4)
)


@dataclass(frozen=True)
class OpsKpiSiteVisitColumns:
    table_name: str
    site_column: str
    site_join_dimension: Literal["site_table_site_id", "kpi_site_id"]
    date_column: str
    value_column: str | None
    value_mode: Literal["sum", "distinct_count", "row_count"] = "sum"


def _sql_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _pg_public_columns(cur, table: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        """,
        (table,),
    )
    return {str(row[0]) for row in cur.fetchall()}


def _first_existing_column(columns: set[str], candidates: tuple[str, ...]) -> str | None:
    lower_to_actual = {c.lower(): c for c in columns}
    for candidate in candidates:
        actual = lower_to_actual.get(candidate.lower())
        if actual is not None:
            return actual
    return None


def detect_ops_kpi_sic_columns(cur) -> OpsKpiSicColumns:
    """Detect the SIC table shape and map it onto the dashboard site×date grain."""

    columns = _pg_public_columns(cur, "ops_kpi_sic")
    if not columns:
        logger.error("ops_kpi_sic: table missing or has no columns")
        raise ValueError("Table public.ops_kpi_sic does not exist or has no columns.")

    date_column = _first_existing_column(
        columns,
        (
            "date",
            "sic_date",
            "event_date",
            "report_date",
            "created_date",
            "outage_start",
            "created_at",
            "started_at",
        ),
    )
    value_column = _first_existing_column(
        columns,
        ("sic_count", "sic", "sic_value", "count", "value", "total", "qty", "quantity"),
    )
    site_column = _first_existing_column(
        columns,
        ("site_id", "site", "kpi_site_id", "pla_id", "pla"),
    )
    value_mode: Literal["sum", "distinct_count", "row_count"] = "sum"
    if value_column is None and "ticket_id" in {c.lower() for c in columns}:
        value_column = _first_existing_column(columns, ("ticket_id",))
        value_mode = "distinct_count"
    elif value_column is None:
        value_mode = "row_count"

    if date_column is None or site_column is None:
        msg = (
            "Table public.ops_kpi_sic must include recognizable site and date columns. "
            "A SIC value column is optional when ticket rows can be counted. "
            f"Detected columns: {', '.join(sorted(columns))}"
        )
        logger.error("ops_kpi_sic schema validation failed: %s", msg)
        raise ValueError(msg)

    site_join_dimension: Literal["site_table_site_id", "kpi_site_id"]
    if site_column.lower() in {"site_id", "site"}:
        site_join_dimension = "site_table_site_id"
    else:
        site_join_dimension = "kpi_site_id"
    sic_cols = OpsKpiSicColumns(
        site_column=site_column,
        site_join_dimension=site_join_dimension,
        date_column=date_column,
        value_column=value_column,
        value_mode=value_mode,
    )
    logger.info(
        "ops_kpi_sic columns: site=%s date=%s value=%s mode=%s join=%s",
        sic_cols.site_column,
        sic_cols.date_column,
        sic_cols.value_column,
        sic_cols.value_mode,
        sic_cols.site_join_dimension,
    )
    return sic_cols


def detect_ops_kpi_site_visit_columns(cur) -> OpsKpiSiteVisitColumns:
    """Find a public site-visit fact table and map it onto the dashboard site×date grain."""

    last_detail = ""
    for table_name in SITE_VISIT_TABLE_CANDIDATES:
        if not _pg_table_exists(cur, table_name):
            logger.debug("site visit candidate %s: table does not exist", table_name)
            continue
        columns = _pg_public_columns(cur, table_name)
        if not columns:
            last_detail = f"{table_name}: no columns"
            logger.debug("site visit candidate %s: %s", table_name, last_detail)
            continue

        date_column = _first_existing_column(
            columns,
            (
                "date",
                "visit_date",
                "site_visit_date",
                "event_date",
                "report_date",
                "created_date",
                "created_at",
                "started_at",
            ),
        )
        value_column = _first_existing_column(
            columns,
            (
                "visit_count",
                "site_visit_count",
                "visits",
                "visitor_count",
                "count",
                "value",
                "total",
                "qty",
                "quantity",
            ),
        )
        site_column = _first_existing_column(
            columns,
            ("site_id", "site", "kpi_site_id", "pla_id", "pla"),
        )
        value_mode: Literal["sum", "distinct_count", "row_count"] = "sum"
        if value_column is None and "ticket_id" in {c.lower() for c in columns}:
            value_column = _first_existing_column(columns, ("ticket_id",))
            value_mode = "distinct_count"
        elif value_column is None:
            value_mode = "row_count"

        if date_column is None or site_column is None:
            last_detail = (
                f"{table_name}: missing site/date (have {', '.join(sorted(columns))})"
            )
            logger.debug("site visit candidate %s: %s", table_name, last_detail)
            continue

        site_join_dimension: Literal["site_table_site_id", "kpi_site_id"]
        if site_column.lower() in {"site_id", "site"}:
            site_join_dimension = "site_table_site_id"
        else:
            site_join_dimension = "kpi_site_id"
        sv_cols = OpsKpiSiteVisitColumns(
            table_name=table_name,
            site_column=site_column,
            site_join_dimension=site_join_dimension,
            date_column=date_column,
            value_column=value_column,
            value_mode=value_mode,
        )
        logger.info(
            "site visit table %s: site=%s date=%s value=%s mode=%s join=%s",
            sv_cols.table_name,
            sv_cols.site_column,
            sv_cols.date_column,
            sv_cols.value_column,
            sv_cols.value_mode,
            sv_cols.site_join_dimension,
        )
        return sv_cols

    msg = (
        "No recognizable site visit fact table in public schema. "
        "Expected one of: "
        + ", ".join(SITE_VISIT_TABLE_CANDIDATES)
        + ". "
        + (last_detail or "None of these tables exist.")
    )
    logger.warning("site visit detection failed: %s", msg)
    raise ValueError(msg)


def _ops_kpi_load_sql(
    *,
    has_ops_kpi_cm_count: bool,
    sic_columns: OpsKpiSicColumns,
    site_visit_columns: OpsKpiSiteVisitColumns | None = None,
) -> str:
    """Build site×date load SQL; CM inner metric uses ``cm_count`` column when present else row count."""
    cm_inner = (
        "COALESCE(e.cm_count, 0)::integer AS cm_row_value"
        if has_ops_kpi_cm_count
        else "1::integer AS cm_row_value"
    )
    sic_site = _sql_ident(sic_columns.site_column)
    sic_date = _sql_ident(sic_columns.date_column)
    sic_value = _sql_ident(sic_columns.value_column) if sic_columns.value_column else None
    if sic_columns.value_mode == "sum":
        assert sic_value is not None
        sic_count_expr = (
            f"SUM(COALESCE(NULLIF(BTRIM(sic.{sic_value}::text), '')::numeric, 0))::integer"
        )
    elif sic_columns.value_mode == "distinct_count":
        assert sic_value is not None
        sic_count_expr = (
            f"COUNT(DISTINCT NULLIF(BTRIM(sic.{sic_value}::text), ''))::integer"
        )
    else:
        sic_count_expr = "COUNT(*)::integer"
    sic_join_column = (
        "sd.site_table_site_id"
        if sic_columns.site_join_dimension == "site_table_site_id"
        else "sd.kpi_site_id"
    )

    date_dim_union_sv = ""
    site_visit_suffix = ""
    site_visit_select = '0::integer AS "Site Visit Count"'
    site_visit_join = ""

    if site_visit_columns is not None:
        sv_site = _sql_ident(site_visit_columns.site_column)
        sv_date = _sql_ident(site_visit_columns.date_column)
        sv_val = (
            _sql_ident(site_visit_columns.value_column)
            if site_visit_columns.value_column
            else None
        )
        sv_table_ref = "public." + _sql_ident(site_visit_columns.table_name)
        date_dim_union_sv = f"""
        UNION
        SELECT sv.{sv_date}::date AS dt FROM {sv_table_ref} sv WHERE sv.{sv_date} IS NOT NULL"""

        if site_visit_columns.value_mode == "sum":
            assert sv_val is not None
            visit_expr = (
                f"SUM(COALESCE(NULLIF(BTRIM(sv.{sv_val}::text), '')::numeric, 0))::integer"
            )
        elif site_visit_columns.value_mode == "distinct_count":
            assert sv_val is not None
            visit_expr = (
                f"COUNT(DISTINCT NULLIF(BTRIM(sv.{sv_val}::text), ''))::integer"
            )
        else:
            visit_expr = "COUNT(*)::integer"

        svc_join = (
            "sd.site_table_site_id"
            if site_visit_columns.site_join_dimension == "site_table_site_id"
            else "sd.kpi_site_id"
        )

        site_visit_suffix = f""",
site_visit_counts AS (
    SELECT
        BTRIM(sv.{sv_site}::text) AS site_id,
        sv.{sv_date}::date AS date,
        {visit_expr} AS site_visit_count
    FROM {sv_table_ref} sv
    WHERE sv.{sv_date} IS NOT NULL
      AND NULLIF(BTRIM(sv.{sv_site}::text), '') IS NOT NULL
    GROUP BY BTRIM(sv.{sv_site}::text), sv.{sv_date}::date
)"""

        site_visit_select = 'COALESCE(svc.site_visit_count, 0) AS "Site Visit Count"'
        site_visit_join = f"""LEFT JOIN site_visit_counts svc
    ON svc.site_id = {svc_join}
   AND svc.date = d.date"""

    return f"""
-- Site-first dataset: every site across the dashboard date axis, with blank metrics when no
-- matching availability fact row exists for (kpi_site_id, date).
-- Date axis includes any calendar day present in availability, CM, or SIC so FY table
-- totals include full-year facts. Charts use a rolling monthly axis ending at the latest
-- month present in loaded data.
WITH site_dim AS (
    SELECT DISTINCT ON (
        COALESCE(
            NULLIF(BTRIM(s.pla_id::text), ''),
            s.site_id::text
        )
    )
        s.site_id::text AS site_table_site_id,
        COALESCE(
            NULLIF(BTRIM(s.pla_id::text), ''),
            s.site_id::text
        ) AS kpi_site_id,
        UPPER(BTRIM(COALESCE(s.region::text, ''))) AS site_region,
        NULLIF(BTRIM(COALESCE(s.zoo::text, '')), '') AS site_zoo,
        NULLIF(BTRIM(COALESCE(s.teritory::text, '')), '') AS site_teritory
    FROM site s
    ORDER BY
        COALESCE(
            NULLIF(BTRIM(s.pla_id::text), ''),
            s.site_id::text
        ),
        s.site_id::text
),
date_dim AS (
    SELECT DISTINCT u.dt::date AS date
    FROM (
        SELECT a.date::date AS dt FROM ops_kpi_availability a
        UNION
        SELECT e.event_date::date AS dt FROM ops_kpi_cm e WHERE e.event_date IS NOT NULL
        UNION
        SELECT sic.{sic_date}::date AS dt FROM ops_kpi_sic sic WHERE sic.{sic_date} IS NOT NULL{date_dim_union_sv}
    ) u
),
cm_counts AS (
    SELECT
        kpi_site_id AS site_id,
        event_date AS date,
        SUM(cm_row_value)::integer AS cm_count
    FROM (
        SELECT
            e.event_date,
            {cm_inner},
            COALESCE(
                NULLIF(BTRIM(s.pla_id), ''),
                s.site_id,
                e.site_id
            ) AS kpi_site_id
        FROM ops_kpi_cm e
        LEFT JOIN site s ON s.site_id = e.site_id
        WHERE e.event_date IS NOT NULL
    ) mapped
    GROUP BY kpi_site_id, event_date
),
sic_counts AS (
    SELECT
        BTRIM(sic.{sic_site}::text) AS site_id,
        sic.{sic_date}::date AS date,
        {sic_count_expr} AS sic_count
    FROM ops_kpi_sic sic
    WHERE sic.{sic_date} IS NOT NULL
      AND NULLIF(BTRIM(sic.{sic_site}::text), '') IS NOT NULL
    GROUP BY BTRIM(sic.{sic_site}::text), sic.{sic_date}::date
){site_visit_suffix}
SELECT
    COALESCE(a.site_id, sd.kpi_site_id) AS site_id,
    sd.site_table_site_id AS site_table_site_id,
    d.date::timestamp AS "Date",
    COALESCE(
        NULLIF(UPPER(BTRIM(a.region)), ''),
        NULLIF(sd.site_region, '')
    ) AS "Region",
    a.pla_id AS "PLA ID",
    a.ptci_number AS "PTCI Number",
    a.incident_count AS "Incident_count",
    a.outage_mins AS "Outage_mins",
    a.accepted_outage_minutes AS "Accepted Outage Minutes",
    a.availability AS "Availability",
    a.uptime_per_tenant AS "Uptime_per_tenant",
    a.total_available_minutes AS "Total Available Minutes",
    COALESCE(sic.sic_count, 0) AS "SIC Count",
    {site_visit_select},
    COALESCE(c.cm_count, 0) AS "CM Count",
    COALESCE(NULLIF(BTRIM(a.zoo), ''), sd.site_zoo) AS "Zoo",
    sd.site_teritory AS site_teritory,
    COALESCE(NULLIF(BTRIM(a.territory), ''), sd.site_teritory) AS "Teritory",
    (a.site_id IS NOT NULL) AS has_availability_row
FROM site_dim sd
CROSS JOIN date_dim d
LEFT JOIN ops_kpi_availability a
    ON a.site_id = sd.kpi_site_id
   AND a.date = d.date
LEFT JOIN sic_counts sic
    ON sic.site_id = {sic_join_column}
   AND sic.date = d.date
{site_visit_join}
LEFT JOIN cm_counts c
    ON c.site_id = sd.kpi_site_id
   AND c.date = d.date
"""

_DATA_DIR = Path(__file__).resolve().parent
DEFAULT_TERRITORY_XLSX = "4_months_dg_run1.xlsx"
COL_DG_SITE_ID = "SiteID"
COL_TERRITORY = "Teritory"


# Columns required for prepare_daily_availability_dataframe (ETL + DB loader output shape).
CSV_REQUIRED_COLUMNS = {
    "Date",
    "Region",
    "PLA ID",
    "PTCI Number",
    "Incident_count",
    "Outage_mins",
    "Accepted Outage Minutes",
    "Availability",
    "Uptime_per_tenant",
    "Total Available Minutes",
    "SIC Count",
    "Site Visit Count",
    "CM Count",
    "Zoo",
}

REGION_ORDER = ["NCR", "NLZ", "SLZ", "VIS", "MIN"]
# Buckets for ops_kpi_availability.region after trim/alias; unknown/blank → OTHER (matches SQL).
REGION_OTHER = "OTHER"
# Overall + five regions + OTHER; table may omit OTHER when empty (see regions_for_table).
CHART_ROW_ORDER = ["Overall", *REGION_ORDER, REGION_OTHER]
CHART_MONTH_START = pd.Period("2025-01", freq="M")
CHART_MONTH_COUNT = 12


def normalize_ops_kpi_region_display(raw: object) -> str:
    """Map raw region text to NCR/NLZ/SLZ/VIS/MIN or OTHER (must stay aligned with _OPS_KPI_REGION_DISPLAY_SQL)."""

    if raw is None:
        return REGION_OTHER
    try:
        if pd.isna(raw):
            return REGION_OTHER
    except (ValueError, TypeError):
        pass
    s = str(raw).strip().upper()
    if s in ("", "NAN", "NONE", "<NA>"):
        return REGION_OTHER
    if s == "METRO MANILA":
        return "NCR"
    if s in REGION_ORDER:
        return s
    return REGION_OTHER


def regions_for_table(df: pd.DataFrame) -> list[str]:
    """Primary regions in fixed order, then OTHER when any row maps to OTHER."""
    regions = list(REGION_ORDER)
    if not df.empty and (df["Region"] == REGION_OTHER).any():
        regions.append(REGION_OTHER)
    return regions

_DEFAULT_T = default_ops_kpi_targets()
EVENT_TARGET_FACTOR = _DEFAULT_T.events_baseline_factor
MTTR_TARGET_MINUTES = int(_DEFAULT_T.mttr_minutes)
AVAILABILITY_TARGET = _DEFAULT_T.availability_pct
NA_VALUE = "N/A"
UNMAPPED_ZOO = "Unmapped"


def _canonical_site_id_series(series: pd.Series) -> pd.Series:
    """Normalize site_id strings for counting: strip; drop placeholders; collapse ``123.0``/``123.000`` to ``123`` (string-safe, no float)."""

    def one(v: object) -> object:
        try:
            if pd.isna(v):
                return pd.NA
        except (ValueError, TypeError):
            if v is None:
                return pd.NA
        s = str(v).strip()
        if s in ("", "nan", "None", "<NA>"):
            return pd.NA
        m = re.fullmatch(r"(\d+)\.(0+)", s)
        if m:
            return m.group(1)
        return s

    return series.map(one)


def load_dashboard_payload(
    database_url: str,
    *,
    targets_database_url: str | None = None,
) -> dict:
    with log_timing(logger, "load_dashboard_payload.load_df"):
        df = load_daily_availability_from_database(database_url)
    tgt_url = (
        targets_database_url
        if targets_database_url is not None
        else database_url
    )
    targets = load_ops_kpi_targets(tgt_url)
    with log_timing(logger, "load_dashboard_payload.build_periods", rows=len(df)):
        periods = build_periods(df)

    grouping_col = (
        "territory_chart_group"
        if "territory_chart_group" in df.columns
        else "Teritory"
    )
    territory_order = sorted(
        {
            str(x).strip()
            for x in df[grouping_col].dropna().unique()
            if str(x).strip() != ""
        }
    )
    with log_timing(logger, "load_dashboard_payload.chart_sql"):
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                events_month_periods, events_month_labels = _chart_month_axes_for_payload(
                    df, cur
                )
                (
                    scope_outages,
                    scope_mttr,
                    scope_avail,
                    terr_outages,
                    terr_mttr,
                    terr_avail,
                ) = fetch_monthly_charts_from_availability_only(
                    cur, events_month_periods, territory_order
                )
                ops_kpi_cubes = fetch_ops_kpi_availability_cubes(cur)

    with log_timing(logger, "load_dashboard_payload.build_table"):
        period_ops_index = build_period_ops_index(df, periods)
        table_rows = build_table_rows(
            df,
            periods,
            targets,
            ops_kpi_cubes=ops_kpi_cubes,
            period_ops_index=period_ops_index,
        )
        table_footer = build_table_row(
            scope_frame(df, "Overall"),
            periods,
            targets,
            label="TOTAL",
            row_kind="footer",
            region="Overall",
            level=0,
            sort_order=999999,
            group_start=False,
            group_end=True,
            ops_kpi_table_actuals=table_ops_actuals_for_row(
                ops_kpi_cubes,
                period_ops_index,
                row_kind="footer",
                region="Overall",
                zoo=None,
            ),
        )

        territory_order, territory_charts = build_territory_charts(
            df,
            periods,
            targets,
            events_month_periods=events_month_periods,
            events_month_labels=events_month_labels,
            events_actuals_by_territory=terr_outages,
            mttr_actuals_by_territory=terr_mttr,
            availability_actuals_by_territory=terr_avail,
        )

    return {
        "meta": build_meta(df, periods, targets),
        "table": {
            "rows": table_rows,
            "footer": table_footer,
        },
        "charts": build_charts(
            df,
            periods,
            targets,
            events_month_periods=events_month_periods,
            events_month_labels=events_month_labels,
            events_actuals_by_scope=scope_outages,
            mttr_actuals_by_scope=scope_mttr,
            availability_actuals_by_scope=scope_avail,
        ),
        "territoryOrder": territory_order,
        "territoryCharts": territory_charts,
    }


def load_daily_availability_from_database(database_url: str) -> pd.DataFrame:
    with log_timing(logger, "load_daily_availability_from_database"):
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                has_cm = _pg_column_exists(cur, "ops_kpi_cm", "cm_count")
                sic_columns = detect_ops_kpi_sic_columns(cur)
                sv_columns = detect_ops_kpi_site_visit_columns(cur)
                cur.execute(
                    _ops_kpi_load_sql(
                        has_ops_kpi_cm_count=has_cm,
                        sic_columns=sic_columns,
                        site_visit_columns=sv_columns,
                    )
                )
                rows = cur.fetchall()
                desc = cur.description
                columns = [
                    getattr(d, "name", None) or (d[0] if d else "")
                    for d in (desc or [])
                ]
        df = pd.DataFrame(rows, columns=columns)
        prepared = prepare_daily_availability_dataframe(df, territory_source="frame")
    date_min = prepared["Date"].min() if "Date" in prepared.columns and len(prepared) else None
    date_max = prepared["Date"].max() if "Date" in prepared.columns and len(prepared) else None
    logger.info(
        "Loaded daily availability: rows=%d has_cm_count_column=%s date_range=%s..%s",
        len(prepared),
        has_cm,
        date_min,
        date_max,
    )
    return prepared


def ops_kpi_data_fingerprint(database_url: str) -> str:
    """Cache-busting token when the dashboard reads from PostgreSQL (data + targets revision)."""
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*)::bigint, MAX(date) FROM ops_kpi_availability")
            count, max_date = cur.fetchone()
            cm_col = "1" if _pg_column_exists(cur, "ops_kpi_cm", "cm_count") else "0"
            tgt_sig = ""
            try:
                cur.execute(
                    "SELECT COALESCE(MAX(updated_at)::text, '') FROM ops_kpi_targets"
                )
                row = cur.fetchone()
                if row:
                    tgt_sig = row[0] or ""
            except Exception:
                logger.warning(
                    "fingerprint: ops_kpi_targets revision query failed",
                    exc_info=True,
                )
            cm_sig = ""
            try:
                cur.execute(
                    "SELECT COUNT(*)::bigint, COALESCE(MAX(event_timestamp)::text, '') FROM ops_kpi_cm"
                )
                row = cur.fetchone()
                if row:
                    cm_sig = f"{row[0] or 0}|{row[1] or ''}"
            except Exception:
                logger.warning(
                    "fingerprint: ops_kpi_cm stats query failed",
                    exc_info=True,
                )
            sic_sig = ""
            try:
                sic_columns = detect_ops_kpi_sic_columns(cur)
                sic_date = _sql_ident(sic_columns.date_column)
                cur.execute(
                    f"SELECT COUNT(*)::bigint, COALESCE(MAX({sic_date})::text, '') FROM ops_kpi_sic"
                )
                row = cur.fetchone()
                if row:
                    sic_sig = f"{row[0] or 0}|{row[1] or ''}"
            except Exception:
                logger.warning(
                    "fingerprint: ops_kpi_sic stats query failed",
                    exc_info=True,
                )
            sv_sig = ""
            try:
                sv_columns = detect_ops_kpi_site_visit_columns(cur)
                sv_date = _sql_ident(sv_columns.date_column)
                sv_table_ident = "public." + _sql_ident(sv_columns.table_name)
                cur.execute(
                    f"SELECT COUNT(*)::bigint, COALESCE(MAX(sv.{sv_date})::text, '') "
                    f"FROM {sv_table_ident} sv"
                )
                row = cur.fetchone()
                if row:
                    sv_sig = f"{sv_columns.table_name}|{row[0] or 0}|{row[1] or ''}"
            except Exception:
                logger.warning(
                    "fingerprint: site visit stats query failed",
                    exc_info=True,
                )
    max_s = max_date.isoformat() if max_date is not None else ""
    return f"{count}|{max_s}|{tgt_sig}|{cm_sig}|sic={sic_sig}|sv={sv_sig}|cc={cm_col}"


def ops_kpi_targets_revision(database_url: str) -> str:
    """Lightweight fingerprint for ops_kpi_targets only."""
    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(MAX(updated_at)::text, '') FROM ops_kpi_targets"
                )
                row = cur.fetchone()
                return (row[0] or "") if row else ""
    except Exception:
        logger.warning(
            "ops_kpi_targets_revision query failed",
            exc_info=True,
        )
        return ""


def prepare_daily_availability_dataframe(
    data: pd.DataFrame,
    *,
    territory_source: Literal["excel", "frame"] = "excel",
) -> pd.DataFrame:
    """Normalize raw daily rows (ETL or database) into the dashboard frame."""
    data = data.copy()
    if "Site Visit Count" not in data.columns:
        data["Site Visit Count"] = 0

    missing_columns = sorted(CSV_REQUIRED_COLUMNS.difference(data.columns))
    if missing_columns:
        msg = "Data is missing required columns: " + ", ".join(missing_columns)
        logger.error("%s (territory_source=%s)", msg, territory_source)
        raise ValueError(msg)

    logger.debug("prepare_daily_availability_dataframe territory_source=%s", territory_source)
    df = data.copy()

    def _norm_text_or_blank(v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, float) and pd.isna(v):
            return ""
        return str(v).strip()

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"]).copy()

    for column in [
        "Incident_count",
        "Outage_mins",
        "Accepted Outage Minutes",
        "Availability",
        "Uptime_per_tenant",
        "Total Available Minutes",
        "SIC Count",
        "Site Visit Count",
        "CM Count",
    ]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df["Region"] = df["Region"].map(normalize_ops_kpi_region_display)

    has_site_table_site_id = "site_table_site_id" in df.columns
    has_fact_flag = "has_availability_row" in df.columns

    pla = normalize_id_series(df["PLA ID"])
    ptci = normalize_id_series(df["PTCI Number"])
    df["ptci_key"] = ptci
    df["site_key"] = pla.where(pla.notna(), ptci)
    if has_site_table_site_id:
        df["site_table_site_id"] = _canonical_site_id_series(
            df["site_table_site_id"].astype(str)
        )
        df["site_key"] = df["site_key"].where(
            df["site_key"].notna(),
            df["site_table_site_id"],
        )
    if territory_source == "excel":
        df = df[df["site_key"].notna()].copy()
    if "site_id" in df.columns:
        sid = df["site_id"].astype(str).str.strip()
        sid = sid.replace(
            {"": pd.NA, "nan": pd.NA, "None": pd.NA, "<NA>": pd.NA}
        )
        df["site_id"] = _canonical_site_id_series(sid)
    else:
        df["site_id"] = _canonical_site_id_series(df["site_key"].astype(str))
    if territory_source == "excel":
        df = df[df["site_id"].notna()].copy()
    df["ptci_site_id"] = _canonical_site_id_series(df["PTCI Number"])
    df["Zoo"] = (
        df["Zoo"]
        .astype(str)
        .str.strip()
        .replace({"": pd.NA, "nan": pd.NA, "None": pd.NA, "<NA>": pd.NA})
        .fillna(UNMAPPED_ZOO)
    )
    if has_fact_flag:
        df["has_availability_row"] = df["has_availability_row"].fillna(False).astype(bool)
    else:
        df["has_availability_row"] = True

    df["availability_ratio"] = df["Availability"].clip(lower=0, upper=1)
    fallback_uptime = df["Uptime_per_tenant"].clip(lower=0, upper=1)
    df["availability_fallback_ratio"] = fallback_uptime
    df["availability_weight"] = pd.to_numeric(
        df["Total Available Minutes"], errors="coerce"
    )
    df["month_period"] = df["Date"].dt.to_period("M")

    if territory_source == "excel":
        lookup = load_teritory_lookup()
        df["Teritory"] = _map_site_key_to_teritory(df["site_key"], lookup)
        df["territory_chart_group"] = df["Teritory"].map(_norm_text_or_blank)
    else:
        if "Teritory" not in df.columns:
            df["Teritory"] = ""
        df["Teritory"] = (
            df["Teritory"]
            .map(_norm_text_or_blank)
        )
        if "site_teritory" not in df.columns:
            df["site_teritory"] = ""
        df["territory_chart_group"] = df["site_teritory"].map(_norm_text_or_blank)

    return df.reset_index(drop=True)


def normalize_id_series(series: pd.Series) -> pd.Series:
    cleaned = series.astype(str).str.strip()
    cleaned = cleaned.replace(
        {
            "": pd.NA,
            "-": pd.NA,
            "nan": pd.NA,
            "None": pd.NA,
            "<NA>": pd.NA,
        }
    )
    return cleaned


def resolve_teritory_excel_path() -> Path | None:
    env = os.environ.get("OPERATIONS_KPI_TERRITORY_XLSX")
    if env:
        p = Path(env)
        if not p.is_absolute():
            p = _DATA_DIR / p
        return p if p.is_file() else None
    p = _DATA_DIR / DEFAULT_TERRITORY_XLSX
    return p if p.is_file() else None


def load_teritory_lookup(path: Path | None = None) -> dict[str, str]:
    """Unique SiteID -> Teritory from the DG workbook; empty dict if missing or invalid."""
    if path is None:
        resolved = resolve_teritory_excel_path()
        if resolved is None:
            return {}
        path = resolved
    else:
        path = Path(path)
        if not path.is_file():
            return {}

    xl = pd.ExcelFile(path)
    sheet_name = "data" if "data" in xl.sheet_names else xl.sheet_names[0]
    tdf = pd.read_excel(path, sheet_name=sheet_name)
    if COL_DG_SITE_ID not in tdf.columns or COL_TERRITORY not in tdf.columns:
        return {}

    sid = normalize_id_series(tdf[COL_DG_SITE_ID])
    terr_raw = tdf[COL_TERRITORY]
    terr = terr_raw.map(
        lambda x: ""
        if x is None or (isinstance(x, float) and pd.isna(x))
        else str(x).strip()
    )
    work = pd.DataFrame({"k": sid, "t": terr})
    work = work[work["k"].notna()].drop_duplicates(subset=["k"], keep="first")
    return {str(k): (str(t) if t is not None else "") for k, t in zip(work["k"], work["t"])}


def _map_site_key_to_teritory(site_key: pd.Series, lookup: dict[str, str]) -> pd.Series:
    if not lookup:
        return pd.Series([""] * len(site_key), index=site_key.index, dtype=object)

    def one(v: object) -> str:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        s = str(v).strip()
        if s in ("", "nan", "None", "<NA>"):
            return ""
        return lookup.get(s, "")

    return site_key.map(one)


def build_periods(df: pd.DataFrame) -> dict[str, pd.Series]:
    current_year = int(df["Date"].max().year)
    previous_year = current_year - 1

    periods = {
        fy_key(previous_year): df["Date"].dt.year == previous_year,
        fy_key(current_year): df["Date"].dt.year == current_year,
    }
    current_year_months = sorted(
        df.loc[df["Date"].dt.year == current_year, "month_period"].dropna().unique().tolist()
    )
    for month_period in current_year_months:
        periods[period_key(month_period)] = df["month_period"] == month_period

    return periods


def fy_key(year: int) -> str:
    return f"FY{year}"


def period_key(month_period: pd.Period) -> str:
    return month_period.strftime("%b_%y").upper()


def build_visit_compact_periods(df: pd.DataFrame) -> dict[str, pd.Series]:
    """FY2025/FY2026 and their sum for the SIC table column group."""
    y0, y1 = VISIT_TABLE_FY_YEARS
    m0 = df["Date"].dt.year == y0
    m1 = df["Date"].dt.year == y1
    return {
        fy_key(y0): m0,
        fy_key(y1): m1,
        VISIT_TABLE_TOTAL_PERIOD_KEY: m0 | m1,
    }


def build_site_visit_table_periods(df: pd.DataFrame) -> dict[str, pd.Series]:
    """FY years and Q1 monthly columns for the Site Visit table (no TOTAL)."""
    y0, y1 = VISIT_TABLE_FY_YEARS
    periods = {
        fy_key(y0): df["Date"].dt.year == y0,
        fy_key(y1): df["Date"].dt.year == y1,
    }
    for month_period in SITE_VISIT_TABLE_MONTH_PERIODS:
        periods[period_key(month_period)] = df["month_period"] == month_period
    return periods


def site_visit_table_period_order() -> list[str]:
    return [
        fy_key(VISIT_TABLE_FY_YEARS[0]),
        fy_key(VISIT_TABLE_FY_YEARS[1]),
        *[period_key(month_period) for month_period in SITE_VISIT_TABLE_MONTH_PERIODS],
        "TARGET",
    ]


def build_meta(
    df: pd.DataFrame, periods: dict[str, pd.Series], targets: OpsKpiTargets
) -> dict:
    full_order = [*periods.keys(), "TARGET"]
    compact_visit_periods = [
        fy_key(VISIT_TABLE_FY_YEARS[0]),
        fy_key(VISIT_TABLE_FY_YEARS[1]),
        VISIT_TABLE_TOTAL_PERIOD_KEY,
    ]
    return {
        "title": "Operations KPI Dashboard",
        "coverageText": (
            ""
        ),
        "periodOrder": full_order,
        "metricPeriodOrder": {
            "events": full_order,
            "mttr": full_order,
            "availability": full_order,
            "cm": full_order,
            "visit": compact_visit_periods,
            "siteVisit": site_visit_table_period_order(),
        },
        "periodText": "",
        "limitations": [],
        "targetUi": {
            "baselineFactors": {
                "events": targets.events_baseline_factor,
                "cm": targets.cm_baseline_factor,
                "visit": targets.visit_baseline_factor,
                "siteVisit": targets.visit_baseline_factor,
            },
            "mttrMinutes": targets.mttr_minutes,
            "availabilityPct": targets.availability_pct,
            "availabilityPctNcr": targets.availability_pct_ncr,
            "availabilityTargetLabel": (
                f"Target ({targets.availability_pct:.2f}%; NCR {targets.availability_pct_ncr:.2f}%)"
            ),
        },
    }


def build_table_rows(
    df: pd.DataFrame,
    periods: dict[str, pd.Series],
    targets: OpsKpiTargets,
    *,
    ops_kpi_cubes: OpsKpiFactCubes | None = None,
    period_ops_index: dict[str, tuple[Literal["fy", "month"], int | date | None]]
    | None = None,
) -> list[dict]:
    rows: list[dict] = []
    for region_index, region in enumerate(regions_for_table(df)):
        region_df = scope_frame(df, region)
        zoo_names = ordered_zoo_names(region_df)
        region_ops: dict[str, tuple[int, float | None, float | None]] | None = None
        if ops_kpi_cubes is not None and period_ops_index is not None:
            region_ops = table_ops_actuals_for_row(
                ops_kpi_cubes,
                period_ops_index,
                row_kind="region",
                region=region,
                zoo=None,
            )
        rows.append(
            build_table_row(
                region_df,
                periods,
                targets,
                label=region,
                row_kind="region",
                region=region,
                level=0,
                sort_order=region_index * 1000,
                group_start=True,
                group_end=not zoo_names,
                ops_kpi_table_actuals=region_ops,
            )
        )
        for zoo_index, zoo_name in enumerate(zoo_names, start=1):
            zoo_df = region_df.loc[region_df["Zoo"] == zoo_name]
            zoo_ops: dict[str, tuple[int, float | None, float | None]] | None = None
            if ops_kpi_cubes is not None and period_ops_index is not None:
                zoo_ops = table_ops_actuals_for_row(
                    ops_kpi_cubes,
                    period_ops_index,
                    row_kind="zoo",
                    region=region,
                    zoo=zoo_name,
                )
            rows.append(
                build_table_row(
                    zoo_df,
                    periods,
                    targets,
                    label=zoo_name,
                    row_kind="zoo",
                    region=region,
                    level=1,
                    sort_order=region_index * 1000 + zoo_index,
                    group_start=False,
                    group_end=zoo_index == len(zoo_names),
                    ops_kpi_table_actuals=zoo_ops,
                )
            )
    return rows


def ordered_zoo_names(df: pd.DataFrame) -> list[str]:
    if df.empty:
        return []
    names = sorted(name for name in df["Zoo"].dropna().unique().tolist() if name != UNMAPPED_ZOO)
    if (df["Zoo"] == UNMAPPED_ZOO).any():
        names.append(UNMAPPED_ZOO)
    return names


def build_table_row(
    scoped_df: pd.DataFrame,
    periods: dict[str, pd.Series],
    targets: OpsKpiTargets,
    label: str | None = None,
    row_kind: str = "zoo",
    region: str | None = None,
    level: int = 1,
    sort_order: int = 0,
    group_start: bool = False,
    group_end: bool = False,
    *,
    ops_kpi_table_actuals: dict[str, tuple[int, float | None, float | None]]
    | None = None,
) -> dict:
    previous_fy_label = fiscal_year_labels(periods)[0]
    baseline_events = aggregate_event_count_table(scoped_df.loc[periods[previous_fy_label]])
    event_target = (
        baseline_events * targets.events_baseline_factor
        if baseline_events is not None
        else None
    )
    baseline_cm = aggregate_cm_count(scoped_df.loc[periods[previous_fy_label]])
    cm_target = (
        baseline_cm * targets.cm_baseline_factor if baseline_cm is not None else None
    )
    baseline_site_visit = aggregate_site_visit_count_table(
        scoped_df.loc[periods[previous_fy_label]]
    )
    site_visit_target = (
        baseline_site_visit * targets.visit_baseline_factor
        if baseline_site_visit is not None
        else None
    )
    if event_target is not None:
        event_target = round(event_target)
    if cm_target is not None:
        cm_target = round(cm_target)
    if site_visit_target is not None:
        site_visit_target = round(site_visit_target)

    visit_periods = build_visit_compact_periods(scoped_df)
    site_visit_periods = build_site_visit_table_periods(scoped_df)
    if ops_kpi_table_actuals is not None:
        ev_act = {p: ops_kpi_table_actuals[p][0] for p in periods}
        mttr_act = {p: ops_kpi_table_actuals[p][1] for p in periods}
        avail_act = {p: ops_kpi_table_actuals[p][2] for p in periods}
    else:
        ev_act = build_period_actuals(scoped_df, periods, aggregate_event_count_table)
        mttr_act = build_period_actuals(scoped_df, periods, aggregate_mttr_minutes_table)
        avail_act = build_period_actuals(scoped_df, periods, aggregate_availability_pct)
    return {
        "label": label or "",
        "rowKind": row_kind,
        "region": region,
        "level": level,
        "sortOrder": sort_order,
        "groupStart": group_start,
        "groupEnd": group_end,
        "siteCount": build_number_cell(count_unique_sites(scoped_df)),
        "events": build_metric_group(
            actuals=ev_act,
            target=event_target,
            kind="number",
            compare_mode="upper_is_bad",
        ),
        "mttr": build_metric_group(
            actuals=mttr_act,
            target=targets.mttr_minutes,
            kind="number",
            compare_mode="upper_is_bad",
        ),
        "availability": build_metric_group(
            actuals=avail_act,
            target=availability_pct_for_region_scope(region or "Overall", targets),
            kind="percent",
            compare_mode="lower_is_bad",
        ),
        "cm": build_metric_group(
            actuals=build_period_actuals(scoped_df, periods, aggregate_cm_count),
            target=cm_target,
            kind="number",
            compare_mode="upper_is_bad",
        ),
        "siteVisit": build_metric_group(
            actuals=build_period_actuals(
                scoped_df, site_visit_periods, aggregate_site_visit_count_table
            ),
            target=site_visit_target,
            kind="number",
            compare_mode="upper_is_bad",
        ),
        "visit": build_metric_group(
            actuals=build_period_actuals(scoped_df, visit_periods, aggregate_visit_count_table),
            target=None,
            kind="number",
            compare_mode="upper_is_bad",
            include_target=False,
        ),
    }


def build_period_actuals(
    scoped_df: pd.DataFrame,
    periods: dict[str, pd.Series],
    aggregator: Callable[[pd.DataFrame], float | int | None],
) -> dict[str, float | int | None]:
    return {
        period_name: aggregator(scoped_df.loc[period_mask])
        for period_name, period_mask in periods.items()
    }


def fiscal_year_labels(periods: dict[str, pd.Series]) -> tuple[str, str]:
    labels = [period_name for period_name in periods if period_name.startswith("FY")]
    if len(labels) != 2:
        raise ValueError(f"Expected exactly two fiscal year labels, found: {labels}")
    return labels[0], labels[1]


def build_metric_group(
    actuals: dict[str, float | int | None],
    target: float | int | None,
    kind: str,
    compare_mode: str,
    *,
    include_target: bool = True,
) -> dict:
    comparison_target = target if include_target else None
    built = {}
    for period, value in actuals.items():
        class_name = ""
        if value is not None and comparison_target is not None:
            if compare_mode == "upper_is_bad" and value > comparison_target:
                class_name = "text-warning"
            elif compare_mode == "lower_is_bad" and value < comparison_target:
                class_name = "text-danger"
        built[period] = build_value_cell(value, kind=kind, class_name=class_name)

    if include_target:
        built["TARGET"] = build_value_cell(target, kind=kind)
    return built


def build_value_cell(
    value: float | int | None,
    *,
    kind: str = "number",
    class_name: str = "",
) -> dict:
    return {
        "text": format_value(value, kind=kind),
        "className": class_name,
        "isNA": value is None,
    }


def build_number_cell(value: int | None) -> dict:
    return build_value_cell(value, kind="number")


def format_value(value: float | int | None, kind: str = "number") -> str:
    if value is None:
        return NA_VALUE
    try:
        probe = float(value)
    except (TypeError, ValueError, OverflowError):
        return str(value)
    if not math.isfinite(probe):
        value = 0.0
    if kind == "percent":
        return f"{value:.2f}%"
    if kind == "number":
        return f"{int(round(value)):,}"
    if kind == "decimal":
        return f"{value:.2f}"
    return str(value)


def count_unique_sites(df: pd.DataFrame) -> int | None:
    """Distinct ``site.site_id`` from the joined ``Site`` table (``site_table_site_id``).

    When loading from PostgreSQL, the site×date load query supplies ``site_table_site_id`` via
    ``public.site`` (same KPI-key mapping as CM events). Same row scope as other metrics:
    ``Region`` in ``REGION_ORDER`` or ``OTHER``, ``Zoo != UNMAPPED_ZOO``, ``scope_frame`` for region rows.

    CSV / offline frames without ``site_table_site_id`` fall back to distinct canonical PTCI
    (``ptci_site_id``) for backwards compatibility.
    """
    if df.empty:
        return 0
    if "site_table_site_id" in df.columns:
        s_site = df["site_table_site_id"]
        if s_site.notna().any():
            return int(s_site.nunique(dropna=True))
    if "ptci_site_id" not in df.columns:
        return 0
    s = df["ptci_site_id"]
    return int(s.nunique(dropna=True))


def _fact_rows_only(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    if "has_availability_row" not in df.columns:
        return df
    return df.loc[df["has_availability_row"]]


def aggregate_cm_count(df: pd.DataFrame) -> int | None:
    """Sum CM across every site×date row in ``df`` (same cells as the PostgreSQL load query).

    CM is intentionally **not** filtered with ``_fact_rows_only``: a CM on a day without an
    ``ops_kpi_availability`` row still contributes, matching ``ops_kpi_cm`` (+ site KPI key)
    rather than tying CM to availability coverage.
    """
    if df.empty:
        return None
    return int(df["CM Count"].fillna(0).sum())


def aggregate_event_count_table(df: pd.DataFrame) -> int | None:
    """Table totals: sum incidents on every site×date row (not restricted to availability facts)."""

    if df.empty:
        return None
    return int(df["Incident_count"].fillna(0).sum())


def aggregate_visit_count_table(df: pd.DataFrame) -> int | None:
    """Table totals: sum SIC on every site×date row (not restricted to availability facts)."""

    if df.empty:
        return None
    return int(df["SIC Count"].fillna(0).sum())


def aggregate_site_visit_count_table(df: pd.DataFrame) -> int | None:
    """Table totals: sum site visits on every site×date row (not restricted to availability facts)."""

    if df.empty:
        return None
    return int(df["Site Visit Count"].fillna(0).sum())


def aggregate_mttr_minutes(df: pd.DataFrame) -> float | None:
    fact_df = _fact_rows_only(df)
    if fact_df.empty:
        return None

    values = fact_df.loc[
        fact_df["Accepted Outage Minutes"] > 0, "Accepted Outage Minutes"
    ].dropna()
    if values.empty:
        return None
    return float(values.mean())


def aggregate_mttr_minutes_table(df: pd.DataFrame) -> float:
    """Mean accepted outage minutes for rows with positive outages; table shows ``0`` when undefined."""

    fact_df = _fact_rows_only(df)
    if fact_df.empty:
        return 0.0

    values = fact_df.loc[
        fact_df["Accepted Outage Minutes"] > 0, "Accepted Outage Minutes"
    ].dropna()
    if values.empty:
        return 0.0

    m = float(values.mean())
    return m if math.isfinite(m) else 0.0


def aggregate_availability_pct(df: pd.DataFrame) -> float | None:
    fact_df = _fact_rows_only(df)
    if fact_df.empty:
        return None

    weighted = fact_df.loc[
        fact_df["availability_ratio"].notna()
        & fact_df["availability_weight"].gt(0, fill_value=False)
    ]
    if not weighted.empty:
        weighted_sum = (weighted["availability_ratio"] * weighted["availability_weight"]).sum()
        total_weight = weighted["availability_weight"].sum()
        if total_weight > 0:
            return float(weighted_sum / total_weight * 100)

    fallback = fact_df["availability_fallback_ratio"].dropna()
    if fallback.empty:
        return None
    return float(fallback.mean() * 100)


def _rolling_chart_month_axes(
    end_period: pd.Period,
) -> tuple[pd.PeriodIndex, list[str]]:
    start_period = end_period - (CHART_MONTH_COUNT - 1)
    events_month_periods = pd.period_range(
        start=start_period, end=end_period, freq="M"
    )
    events_month_labels = [
        period.strftime("%b %Y") for period in events_month_periods.to_timestamp()
    ]
    return events_month_periods, events_month_labels


def _chart_month_axes(df: pd.DataFrame) -> tuple[pd.PeriodIndex, list[str]]:
    if df.empty or "month_period" not in df.columns:
        return pd.PeriodIndex([], freq="M"), []
    end_period = df["month_period"].max()
    if pd.isna(end_period):
        return pd.PeriodIndex([], freq="M"), []
    return _rolling_chart_month_axes(end_period)


def _chart_month_axes_for_payload(
    df: pd.DataFrame,
    cur,
) -> tuple[pd.PeriodIndex, list[str]]:
    """Chart month axis: rolling 12 months ending at latest merged df/availability month."""

    cur.execute("SELECT MAX(date) FROM ops_kpi_availability")
    row = cur.fetchone()
    max_date = row[0] if row else None

    end_candidates: list[pd.Period] = []
    if not df.empty and "month_period" in df.columns:
        mx = df["month_period"].max()
        if pd.notna(mx):
            end_candidates.append(mx)
    if max_date is not None:
        end_candidates.append(pd.Timestamp(max_date).to_period("M"))

    if not end_candidates:
        return pd.PeriodIndex([], freq="M"), []

    end_period = max(end_candidates)
    return _rolling_chart_month_axes(end_period)


def _align_monthly_incident_sums_to_list(
    events_month_periods: pd.PeriodIndex,
    month_to_sum: dict,
) -> list[int]:
    """Map SQL ``month_start`` dates to chart month periods; missing months → 0."""

    out: list[int] = []
    for period in events_month_periods:
        key = period.to_timestamp().normalize().date()
        raw = month_to_sum.get(key)
        if raw is None:
            out.append(0)
            continue
        v = float(raw)
        if not math.isfinite(v):
            out.append(0)
        else:
            out.append(int(round(v)))
    return out


def _align_monthly_mttr_to_list(
    events_month_periods: pd.PeriodIndex,
    month_to_mean: dict,
) -> list[float | None]:
    """Map SQL month buckets to chart periods; missing months → ``None``. Values rounded to 2 dp."""

    out: list[float | None] = []
    for period in events_month_periods:
        key = period.to_timestamp().normalize().date()
        raw = month_to_mean.get(key)
        if raw is None:
            out.append(None)
            continue
        v = float(raw)
        if not math.isfinite(v):
            out.append(None)
        else:
            out.append(round(v, 2))
    return out


def _align_monthly_availability_to_list(
    events_month_periods: pd.PeriodIndex,
    month_to_pct: dict,
) -> list[float | None]:
    """Match ``monthly_availability`` rounding (4 dp); missing months → ``None``."""

    out: list[float | None] = []
    for period in events_month_periods:
        key = period.to_timestamp().normalize().date()
        raw = month_to_pct.get(key)
        if raw is None:
            out.append(None)
            continue
        v = float(raw)
        if not math.isfinite(v):
            out.append(None)
        else:
            out.append(round(v, 4))
    return out


# Weighted ``availability`` x ``total_available_minutes``, else mean clipped ``uptime_per_tenant``
# (parity with ``aggregate_availability_pct`` on ``ops_kpi_availability`` rows).
_AVAILABILITY_PCT_SQL = """
(
  CASE
    WHEN SUM(
      CASE
        WHEN availability IS NOT NULL
         AND COALESCE(total_available_minutes, 0) > 0
        THEN COALESCE(total_available_minutes, 0)
        ELSE 0
      END
    ) > 0
    THEN
      SUM(
        CASE
          WHEN availability IS NOT NULL
           AND COALESCE(total_available_minutes, 0) > 0
          THEN LEAST(GREATEST(availability, 0), 1)
               * COALESCE(total_available_minutes, 0)
          ELSE 0
        END
      )
      / NULLIF(
          SUM(
            CASE
              WHEN availability IS NOT NULL
               AND COALESCE(total_available_minutes, 0) > 0
              THEN COALESCE(total_available_minutes, 0)
              ELSE 0
            END
          ),
          0
        )
      * 100.0
    ELSE
      ( AVG(
          LEAST(GREATEST(COALESCE(uptime_per_tenant, 0), 0), 1)
        ) FILTER (WHERE uptime_per_tenant IS NOT NULL)
      ) * 100.0
  END
)::double precision"""

# Same MTTR expression as monthly chart SQL (``ops_kpi_availability`` only).
MTTR_AVG_EXPR = """AVG(accepted_outage_minutes) FILTER (
                   WHERE accepted_outage_minutes IS NOT NULL
                     AND accepted_outage_minutes > 0
               )::double precision"""

# Dashboard region bucket: METRO MANILA→NCR; known five codes; else OTHER (must match normalize_ops_kpi_region_display).
_OPS_KPI_REGION_DISPLAY_SQL = """(
  CASE
    WHEN UPPER(TRIM(BTRIM(COALESCE(region, '')))) = 'METRO MANILA' THEN 'NCR'
    WHEN UPPER(TRIM(BTRIM(COALESCE(region, '')))) IN ('NCR', 'NLZ', 'SLZ', 'VIS', 'MIN')
      THEN UPPER(TRIM(BTRIM(COALESCE(region, ''))))
    ELSE 'OTHER'
  END
)"""
_OPS_KPI_ZOO_KEY_SQL = "TRIM(BTRIM(COALESCE(zoo, '')))"


@dataclass(frozen=True)
class OpsKpiFactCubes:
    """Pre-aggregated ``ops_kpi_availability`` metrics for dashboard table cells."""

    year_overall: dict[int, tuple[float, float | None, float | None]]
    year_region: dict[tuple[int, str], tuple[float, float | None, float | None]]
    year_zoo: dict[tuple[int, str, str], tuple[float, float | None, float | None]]
    month_overall: dict[date, tuple[float, float | None, float | None]]
    month_region: dict[tuple[date, str], tuple[float, float | None, float | None]]
    month_zoo: dict[tuple[date, str, str], tuple[float, float | None, float | None]]


def _ops_kpi_raw_tuple_from_row(
    inc: object, mttr: object, apct: object
) -> tuple[float, float | None, float | None]:
    inc_f = float(inc) if inc is not None else 0.0
    if not math.isfinite(inc_f):
        inc_f = 0.0
    mttr_v: float | None
    if mttr is None:
        mttr_v = None
    else:
        m = float(mttr)
        mttr_v = None if not math.isfinite(m) else round(m, 2)
    if apct is None:
        av_v = None
    else:
        a = float(apct)
        av_v = None if not math.isfinite(a) else round(a, 4)
    return (inc_f, mttr_v, av_v)


def _ops_kpi_table_triple(
    raw: tuple[float, float | None, float | None] | None,
) -> tuple[int, float | None, float | None]:
    """Chart-aligned table cells: outages default 0 when absent; MTTR/avail may be None."""
    if raw is None:
        return (0, None, None)
    inc_f, mttr_v, av_v = raw
    ev = int(round(inc_f)) if math.isfinite(inc_f) else 0
    return (ev, mttr_v, av_v)


def fetch_ops_kpi_availability_cubes(cur) -> OpsKpiFactCubes:
    """Six grouped queries over ``ops_kpi_availability`` (same aggregates as chart SQL)."""

    agg = f"""
      SUM(COALESCE(incident_count, 0))::double precision,
      {MTTR_AVG_EXPR},
      {_AVAILABILITY_PCT_SQL}
    """

    cur.execute(
        f"""
        SELECT EXTRACT(YEAR FROM date)::int AS yr, {agg}
        FROM ops_kpi_availability
        GROUP BY 1
        ORDER BY 1
        """
    )
    year_overall: dict[int, tuple[float, float | None, float | None]] = {}
    for row in cur.fetchall():
        yr = int(row[0])
        year_overall[yr] = _ops_kpi_raw_tuple_from_row(row[1], row[2], row[3])

    cur.execute(
        f"""
        SELECT EXTRACT(YEAR FROM date)::int AS yr,
               {_OPS_KPI_REGION_DISPLAY_SQL} AS rk,
               {agg}
        FROM ops_kpi_availability
        GROUP BY 1, 2
        """
    )
    year_region: dict[tuple[int, str], tuple[float, float | None, float | None]] = {}
    for row in cur.fetchall():
        yr, rk = int(row[0]), str(row[1])
        year_region[(yr, rk)] = _ops_kpi_raw_tuple_from_row(row[2], row[3], row[4])

    cur.execute(
        f"""
        SELECT EXTRACT(YEAR FROM date)::int AS yr,
               {_OPS_KPI_REGION_DISPLAY_SQL} AS rk,
               {_OPS_KPI_ZOO_KEY_SQL} AS zk,
               {agg}
        FROM ops_kpi_availability
        GROUP BY 1, 2, 3
        """
    )
    year_zoo: dict[tuple[int, str, str], tuple[float, float | None, float | None]] = {}
    for row in cur.fetchall():
        yr, rk, zk = int(row[0]), str(row[1]), str(row[2])
        year_zoo[(yr, rk, zk)] = _ops_kpi_raw_tuple_from_row(row[3], row[4], row[5])

    cur.execute(
        f"""
        SELECT date_trunc('month', date)::date AS ms, {agg}
        FROM ops_kpi_availability
        GROUP BY 1
        ORDER BY 1
        """
    )
    month_overall: dict[date, tuple[float, float | None, float | None]] = {}
    for row in cur.fetchall():
        ms = row[0]
        if hasattr(ms, "date"):
            ms = ms.date()
        month_overall[ms] = _ops_kpi_raw_tuple_from_row(row[1], row[2], row[3])

    cur.execute(
        f"""
        SELECT date_trunc('month', date)::date AS ms,
               {_OPS_KPI_REGION_DISPLAY_SQL} AS rk,
               {agg}
        FROM ops_kpi_availability
        GROUP BY 1, 2
        """
    )
    month_region: dict[tuple[date, str], tuple[float, float | None, float | None]] = {}
    for row in cur.fetchall():
        ms = row[0]
        if hasattr(ms, "date"):
            ms = ms.date()
        rk = str(row[1])
        month_region[(ms, rk)] = _ops_kpi_raw_tuple_from_row(row[2], row[3], row[4])

    cur.execute(
        f"""
        SELECT date_trunc('month', date)::date AS ms,
               {_OPS_KPI_REGION_DISPLAY_SQL} AS rk,
               {_OPS_KPI_ZOO_KEY_SQL} AS zk,
               {agg}
        FROM ops_kpi_availability
        GROUP BY 1, 2, 3
        """
    )
    month_zoo: dict[tuple[date, str, str], tuple[float, float | None, float | None]] = {}
    for row in cur.fetchall():
        ms = row[0]
        if hasattr(ms, "date"):
            ms = ms.date()
        rk, zk = str(row[1]), str(row[2])
        month_zoo[(ms, rk, zk)] = _ops_kpi_raw_tuple_from_row(row[3], row[4], row[5])

    return OpsKpiFactCubes(
        year_overall=year_overall,
        year_region=year_region,
        year_zoo=year_zoo,
        month_overall=month_overall,
        month_region=month_region,
        month_zoo=month_zoo,
    )


def build_period_ops_index(
    df: pd.DataFrame, periods: dict[str, pd.Series]
) -> dict[str, tuple[Literal["fy", "month"], int | date | None]]:
    """Map each table period key to a fiscal year or calendar month start (for cube lookup)."""
    out: dict[str, tuple[Literal["fy", "month"], int | date | None]] = {}
    for name, mask in periods.items():
        if name.startswith("FY"):
            out[name] = ("fy", int(name[2:]))
        else:
            sub = df.loc[mask, "Date"]
            if sub.empty:
                out[name] = ("month", None)
            else:
                ms = (
                    pd.Timestamp(sub.min()).to_period("M").to_timestamp().normalize().date()
                )
                out[name] = ("month", ms)
    return out


def table_ops_actuals_for_row(
    cubes: OpsKpiFactCubes,
    period_ops_index: dict[str, tuple[Literal["fy", "month"], int | date | None]],
    *,
    row_kind: str,
    region: str | None,
    zoo: str | None,
) -> dict[str, tuple[int, float | None, float | None]]:
    """Outages / MTTR / availability actuals per period from pre-fetched cubes."""
    actuals: dict[str, tuple[int, float | None, float | None]] = {}
    for pk, kind_y in period_ops_index.items():
        kind, y_or_ms = kind_y
        raw: tuple[float, float | None, float | None] | None = None
        if kind == "fy":
            assert isinstance(y_or_ms, int)
            yr = y_or_ms
            if row_kind == "footer":
                raw = cubes.year_overall.get(yr)
            elif row_kind == "region" and region is not None:
                raw = cubes.year_region.get((yr, region))
            elif row_kind == "zoo" and region is not None and zoo is not None:
                raw = cubes.year_zoo.get((yr, region, zoo))
        else:
            ms = y_or_ms
            if ms is None:
                raw = None
            elif row_kind == "footer":
                raw = cubes.month_overall.get(ms)
            elif row_kind == "region" and region is not None:
                raw = cubes.month_region.get((ms, region))
            elif row_kind == "zoo" and region is not None and zoo is not None:
                raw = cubes.month_zoo.get((ms, region, zoo))
        actuals[pk] = _ops_kpi_table_triple(raw)
    return actuals


def period_date_range_for_insight(
    df: pd.DataFrame,
    periods: dict[str, pd.Series],
    period_key: str,
    *,
    extra_periods: dict[str, pd.Series] | None = None,
) -> tuple[date | None, date | None]:
    mask_series = periods.get(period_key)
    if mask_series is None and extra_periods is not None:
        mask_series = extra_periods.get(period_key)
    if mask_series is None:
        return (None, None)
    sub = df.loc[mask_series, "Date"]
    if sub.empty:
        return (None, None)
    return (
        pd.Timestamp(sub.min()).date(),
        pd.Timestamp(sub.max()).date(),
    )


def fetch_ops_kpi_metrics_for_date_range(
    cur,
    date_start: date,
    date_end: date,
    *,
    row_kind: str,
    region: str | None,
    zoo: str | None,
) -> tuple[int, float | None, float | None]:
    """Single-period aggregates from ``ops_kpi_availability`` (same expressions as charts)."""

    where = ["date >= %s", "date <= %s"]
    params: list[object] = [date_start, date_end]
    if row_kind == "region":
        if not region:
            raise ValueError("region is required for row_kind region")
        where.append(f"{_OPS_KPI_REGION_DISPLAY_SQL} = %s")
        params.append(region)
    elif row_kind == "zoo":
        if not region or not zoo:
            raise ValueError("region and zoo are required for row_kind zoo")
        where.append(f"{_OPS_KPI_REGION_DISPLAY_SQL} = %s")
        params.append(region)
        where.append(f"{_OPS_KPI_ZOO_KEY_SQL} = %s")
        params.append(zoo)
    elif row_kind != "footer":
        raise ValueError(f"Invalid row_kind: {row_kind}")

    agg = f"""
      SUM(COALESCE(incident_count, 0))::double precision,
      {MTTR_AVG_EXPR},
      {_AVAILABILITY_PCT_SQL}
    """
    cur.execute(
        f"""
        SELECT {agg}
        FROM ops_kpi_availability
        WHERE {' AND '.join(where)}
        """,
        params,
    )
    row = cur.fetchone()
    if not row:
        logger.debug(
            "fetch_ops_kpi_metrics_for_date_range: no rows %s..%s row_kind=%s region=%r zoo=%r",
            date_start,
            date_end,
            row_kind,
            region,
            zoo,
        )
        return (0, None, None)
    raw = _ops_kpi_raw_tuple_from_row(row[0], row[1], row[2])
    triple = _ops_kpi_table_triple(raw)
    logger.debug(
        "fetch_ops_kpi_metrics_for_date_range: %s..%s row_kind=%s -> events=%s",
        date_start,
        date_end,
        row_kind,
        triple[0],
    )
    return triple


def fetch_monthly_charts_from_availability_only(
    cur,
    events_month_periods: pd.PeriodIndex,
    territory_order: list[str],
) -> tuple[
    dict[str, list[int]],
    dict[str, list[float | None]],
    dict[str, list[float | None]],
    dict[str, list[int]],
    dict[str, list[float | None]],
    dict[str, list[float | None]],
]:
    """Outages, MTTR, and availability chart series from ``ops_kpi_availability`` (one query per grain)."""

    n = len(events_month_periods)
    if n == 0:
        empty_s = {s: [] for s in CHART_ROW_ORDER}
        empty_tr = {t: [] for t in territory_order}
        return (
            empty_s,
            {s: [] for s in CHART_ROW_ORDER},
            {s: [] for s in CHART_ROW_ORDER},
            {t: [] for t in territory_order},
            {t: [] for t in territory_order},
            {t: [] for t in territory_order},
        )

    cur.execute(
        f"""
        SELECT date_trunc('month', date)::date AS month_start,
               SUM(COALESCE(incident_count, 0))::double precision,
               {MTTR_AVG_EXPR},
               {_AVAILABILITY_PCT_SQL}
        FROM ops_kpi_availability
        GROUP BY 1
        ORDER BY 1
        """
    )
    overall_inc: dict = {}
    overall_mttr: dict = {}
    overall_avail: dict = {}
    for row in cur.fetchall():
        ms, inc_sum, mttr_avg, apct = row[0], row[1], row[2], row[3]
        overall_inc[ms] = float(inc_sum) if inc_sum is not None else 0.0
        overall_mttr[ms] = None if mttr_avg is None else float(mttr_avg)
        overall_avail[ms] = None if apct is None else float(apct)

    cur.execute(
        f"""
        SELECT date_trunc('month', date)::date AS month_start,
               {_OPS_KPI_REGION_DISPLAY_SQL} AS region_key,
               SUM(COALESCE(incident_count, 0))::double precision,
               {MTTR_AVG_EXPR},
               {_AVAILABILITY_PCT_SQL}
        FROM ops_kpi_availability
        GROUP BY 1, 2
        """
    )
    per_region_inc: dict[str, dict] = {}
    per_region_mttr: dict[str, dict] = {}
    per_region_avail: dict[str, dict] = {}
    for row in cur.fetchall():
        ms, rk, inc_sum, mttr_avg, apct = row[0], row[1], row[2], row[3], row[4]
        per_region_inc.setdefault(rk, {})[ms] = (
            float(inc_sum) if inc_sum is not None else 0.0
        )
        per_region_mttr.setdefault(rk, {})[ms] = (
            None if mttr_avg is None else float(mttr_avg)
        )
        per_region_avail.setdefault(rk, {})[ms] = None if apct is None else float(apct)

    scope_inc: dict[str, list[int]] = {
        "Overall": _align_monthly_incident_sums_to_list(events_month_periods, overall_inc)
    }
    scope_mttr: dict[str, list[float | None]] = {
        "Overall": _align_monthly_mttr_to_list(events_month_periods, overall_mttr)
    }
    scope_avail: dict[str, list[float | None]] = {
        "Overall": _align_monthly_availability_to_list(
            events_month_periods, overall_avail
        )
    }
    for scope in CHART_ROW_ORDER:
        if scope == "Overall":
            continue
        scope_inc[scope] = _align_monthly_incident_sums_to_list(
            events_month_periods, per_region_inc.get(scope, {})
        )
        scope_mttr[scope] = _align_monthly_mttr_to_list(
            events_month_periods, per_region_mttr.get(scope, {})
        )
        scope_avail[scope] = _align_monthly_availability_to_list(
            events_month_periods, per_region_avail.get(scope, {})
        )

    cur.execute(
        f"""
        SELECT date_trunc('month', date)::date AS month_start,
               TRIM(BTRIM(COALESCE(territory, ''))) AS territory_key,
               SUM(COALESCE(incident_count, 0))::double precision,
               {MTTR_AVG_EXPR},
               {_AVAILABILITY_PCT_SQL}
        FROM ops_kpi_availability
        WHERE TRIM(BTRIM(COALESCE(territory, ''))) <> ''
        GROUP BY 1, 2
        """
    )
    per_terr_inc: dict[str, dict] = {}
    per_terr_mttr: dict[str, dict] = {}
    per_terr_avail: dict[str, dict] = {}
    for row in cur.fetchall():
        ms, tk, inc_sum, mttr_avg, apct = row[0], row[1], row[2], row[3], row[4]
        per_terr_inc.setdefault(tk, {})[ms] = (
            float(inc_sum) if inc_sum is not None else 0.0
        )
        per_terr_mttr.setdefault(tk, {})[ms] = (
            None if mttr_avg is None else float(mttr_avg)
        )
        per_terr_avail.setdefault(tk, {})[ms] = None if apct is None else float(apct)

    terr_inc: dict[str, list[int]] = {
        t: _align_monthly_incident_sums_to_list(
            events_month_periods, per_terr_inc.get(t, {})
        )
        for t in territory_order
    }
    terr_mttr: dict[str, list[float | None]] = {
        t: _align_monthly_mttr_to_list(events_month_periods, per_terr_mttr.get(t, {}))
        for t in territory_order
    }
    terr_avail: dict[str, list[float | None]] = {
        t: _align_monthly_availability_to_list(
            events_month_periods, per_terr_avail.get(t, {})
        )
        for t in territory_order
    }

    return scope_inc, scope_mttr, scope_avail, terr_inc, terr_mttr, terr_avail


def _chart_bundle_for_scoped_df(
    scoped_df: pd.DataFrame,
    periods: dict[str, pd.Series],
    targets: OpsKpiTargets,
    events_month_periods: pd.PeriodIndex,
    events_month_labels: list[str],
    *,
    availability_region_for_target: str | None = None,
    events_actual_outages: list[int] | None = None,
    mttr_actual_outages: list[float | None] | None = None,
    availability_actual: list[float | None] | None = None,
) -> dict:
    event_target_series = build_monthly_target_series(
        scoped_df,
        periods,
        aggregate_event_count_table,
        events_month_periods,
        targets.events_baseline_factor,
    )
    cm_target_series = build_monthly_target_series(
        scoped_df,
        periods,
        aggregate_cm_count,
        events_month_periods,
        targets.cm_baseline_factor,
    )
    site_visit_target_series = build_monthly_target_series(
        scoped_df,
        periods,
        aggregate_site_visit_count_table,
        events_month_periods,
        targets.visit_baseline_factor,
    )
    mt = targets.mttr_minutes
    if availability_region_for_target is None:
        av = targets.availability_pct
    else:
        av = availability_pct_for_region_scope(availability_region_for_target, targets)
    events_values = (
        events_actual_outages
        if events_actual_outages is not None
        else monthly_events(scoped_df, events_month_periods)
    )
    mttr_values = (
        mttr_actual_outages
        if mttr_actual_outages is not None
        else monthly_mttr(scoped_df, events_month_periods)
    )
    availability_values = (
        availability_actual
        if availability_actual is not None
        else monthly_availability(scoped_df, events_month_periods)
    )
    return {
        "events": {
            "available": True,
            "months": events_month_labels,
            "actual": events_values,
            "target": event_target_series,
        },
        "mttr": {
            "available": True,
            "months": events_month_labels,
            "actual": mttr_values,
            "target": [mt] * len(events_month_periods),
        },
        "availability": {
            "available": True,
            "months": events_month_labels,
            "actual": availability_values,
            "target": [av] * len(events_month_periods),
        },
        "cm": {
            "available": True,
            "months": events_month_labels,
            "actual": monthly_cm_count(scoped_df, events_month_periods),
            "target": cm_target_series,
        },
        "siteVisit": {
            "available": True,
            "months": events_month_labels,
            "actual": monthly_site_visit_count(scoped_df, events_month_periods),
            "target": site_visit_target_series,
        },
    }


def build_charts(
    df: pd.DataFrame,
    periods: dict[str, pd.Series],
    targets: OpsKpiTargets,
    *,
    events_month_periods: pd.PeriodIndex,
    events_month_labels: list[str],
    events_actuals_by_scope: dict[str, list[int]],
    mttr_actuals_by_scope: dict[str, list[float | None]],
    availability_actuals_by_scope: dict[str, list[float | None]],
) -> dict:
    charts = {}
    for scope in CHART_ROW_ORDER:
        scoped_df = scope_frame(df, scope)
        charts[scope] = _chart_bundle_for_scoped_df(
            scoped_df,
            periods,
            targets,
            events_month_periods,
            events_month_labels,
            availability_region_for_target=scope,
            events_actual_outages=events_actuals_by_scope[scope],
            mttr_actual_outages=mttr_actuals_by_scope[scope],
            availability_actual=availability_actuals_by_scope[scope],
        )
    return charts


def build_territory_charts(
    df: pd.DataFrame,
    periods: dict[str, pd.Series],
    targets: OpsKpiTargets,
    *,
    events_month_periods: pd.PeriodIndex,
    events_month_labels: list[str],
    events_actuals_by_territory: dict[str, list[int]],
    mttr_actuals_by_territory: dict[str, list[float | None]],
    availability_actuals_by_territory: dict[str, list[float | None]],
) -> tuple[list[str], dict[str, dict]]:
    grouping_col = "territory_chart_group"
    if grouping_col not in df.columns:
        grouping_col = "Teritory"
    territory_order = sorted(
        {
            str(x).strip()
            for x in df[grouping_col].dropna().unique()
            if str(x).strip() != ""
        }
    )
    charts: dict[str, dict] = {}
    for t in territory_order:
        scoped_df = df.loc[df[grouping_col].astype(str).str.strip() == t]
        charts[t] = _chart_bundle_for_scoped_df(
            scoped_df,
            periods,
            targets,
            events_month_periods,
            events_month_labels,
            availability_region_for_target=None,
            events_actual_outages=events_actuals_by_territory[t],
            mttr_actual_outages=mttr_actuals_by_territory[t],
            availability_actual=availability_actuals_by_territory[t],
        )
    return territory_order, charts


def monthly_events(df: pd.DataFrame, month_periods: pd.PeriodIndex) -> list[int]:
    """Calendar-month sum of ``Incident_count`` (same semantics as the KPI table)."""

    values: list[int] = []
    for period in month_periods:
        sub = df.loc[df["month_period"] == period]
        if sub.empty:
            values.append(0)
        else:
            v = aggregate_event_count_table(sub)
            values.append(int(v) if v is not None else 0)
    return values


def monthly_cm_count(df: pd.DataFrame, month_periods: pd.PeriodIndex) -> list[int | None]:
    values: list[int | None] = []
    for period in month_periods:
        value = aggregate_cm_count(df.loc[df["month_period"] == period])
        values.append(int(value) if value is not None else None)
    return values


def monthly_site_visit_count(
    df: pd.DataFrame, month_periods: pd.PeriodIndex
) -> list[int | None]:
    values: list[int | None] = []
    for period in month_periods:
        value = aggregate_site_visit_count_table(df.loc[df["month_period"] == period])
        values.append(int(value) if value is not None else None)
    return values


def build_monthly_target_series(
    scoped_df: pd.DataFrame,
    periods: dict[str, pd.Series],
    aggregator: Callable[[pd.DataFrame], float | int | None],
    month_periods: pd.PeriodIndex,
    baseline_factor: float,
) -> list[int | None]:
    previous_fy_label = fiscal_year_labels(periods)[0]
    baseline_value = aggregator(scoped_df.loc[periods[previous_fy_label]])
    previous_fy_month_count = int(
        scoped_df.loc[periods[previous_fy_label], "month_period"].nunique()
    )
    if baseline_value is None or not previous_fy_month_count:
        return [None] * len(month_periods)

    monthly_target = (
        float(baseline_value) * baseline_factor / previous_fy_month_count
    )
    rounded = round(monthly_target)
    return [rounded] * len(month_periods)


def monthly_availability(df: pd.DataFrame, month_periods: pd.PeriodIndex) -> list[float | None]:
    values: list[float | None] = []
    for period in month_periods:
        period_df = df.loc[df["month_period"] == period]
        value = aggregate_availability_pct(period_df)
        values.append(round(value, 4) if value is not None else None)
    return values


def monthly_mttr(df: pd.DataFrame, month_periods: pd.PeriodIndex) -> list[float | None]:
    values: list[float | None] = []
    for period in month_periods:
        period_df = df.loc[df["month_period"] == period]
        value = aggregate_mttr_minutes(period_df)
        values.append(round(value, 2) if value is not None else None)
    return values


def scope_frame(df: pd.DataFrame, scope: str) -> pd.DataFrame:
    if scope == "Overall":
        return df
    return df.loc[df["Region"] == scope]
