from __future__ import annotations

import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pandas as pd
import psycopg


@dataclass(frozen=True)
class OpsKpiTargets:
    """Per-metric baseline factors (prev-FY total × factor) plus fixed MTTR and availability."""

    events_baseline_factor: float
    cm_baseline_factor: float
    visit_baseline_factor: float
    mttr_minutes: float
    availability_pct: float


def default_ops_kpi_targets() -> OpsKpiTargets:
    return OpsKpiTargets(
        events_baseline_factor=0.85,
        cm_baseline_factor=0.85,
        visit_baseline_factor=0.85,
        mttr_minutes=200.0,
        availability_pct=99.96,
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
        return d
    legacy = rows.get("baseline_factor")
    ev = rows.get("events_baseline_factor", legacy if legacy is not None else d.events_baseline_factor)
    cm = rows.get("cm_baseline_factor", legacy if legacy is not None else d.cm_baseline_factor)
    vis = rows.get("visit_baseline_factor", legacy if legacy is not None else d.visit_baseline_factor)
    mt = rows.get("mttr_minutes", d.mttr_minutes)
    ap = rows.get("availability_pct", d.availability_pct)
    return OpsKpiTargets(
        events_baseline_factor=float(ev),
        cm_baseline_factor=float(cm),
        visit_baseline_factor=float(vis),
        mttr_minutes=float(mt),
        availability_pct=float(ap),
    )

OPS_KPI_LOAD_SQL = """
-- Site-first dataset: every site across the dashboard date axis, with blank metrics when no
-- matching availability fact row exists for (kpi_site_id, date).
WITH site_dim AS (
    SELECT
        s.site_id::text AS site_table_site_id,
        COALESCE(
            NULLIF(BTRIM(s.pla_id::text), ''),
            s.site_id::text
        ) AS kpi_site_id,
        UPPER(BTRIM(COALESCE(s.region::text, ''))) AS site_region,
        NULLIF(BTRIM(COALESCE(s.zoo::text, '')), '') AS site_zoo,
        NULLIF(BTRIM(COALESCE(s.teritory::text, '')), '') AS site_teritory
    FROM site s
),
date_dim AS (
    SELECT DISTINCT a.date::date AS date
    FROM ops_kpi_availability a
),
cm_counts AS (
    SELECT
        kpi_site_id AS site_id,
        event_date AS date,
        COUNT(*)::integer AS cm_count
    FROM (
        SELECT
            e.event_date,
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
)
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
    COALESCE(v.visit_count, 0) AS "Visit Count",
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
LEFT JOIN ops_kpi_sitevisit v
    ON v.site_id = sd.kpi_site_id
   AND v.date = d.date
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
    "Visit Count",
    "CM Count",
    "Zoo",
}

REGION_ORDER = ["NCR", "NLZ", "SLZ", "VIS", "MIN"]
CHART_ROW_ORDER = ["Overall", *REGION_ORDER]
CHART_MONTH_START = pd.Period("2025-08", freq="M")

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
    df = load_daily_availability_from_database(database_url)
    tgt_url = (
        targets_database_url
        if targets_database_url is not None
        else database_url
    )
    targets = load_ops_kpi_targets(tgt_url)
    periods = build_periods(df)
    table_rows = build_table_rows(df, periods, targets)
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
    )

    territory_order, territory_charts = build_territory_charts(df, periods, targets)

    return {
        "meta": build_meta(df, periods, targets),
        "table": {
            "rows": table_rows,
            "footer": table_footer,
        },
        "charts": build_charts(df, periods, targets),
        "territoryOrder": territory_order,
        "territoryCharts": territory_charts,
    }


def load_daily_availability_from_database(database_url: str) -> pd.DataFrame:
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(OPS_KPI_LOAD_SQL)
            rows = cur.fetchall()
            desc = cur.description
            columns = [
                getattr(d, "name", None) or (d[0] if d else "")
                for d in (desc or [])
            ]
    df = pd.DataFrame(rows, columns=columns)
    return prepare_daily_availability_dataframe(df, territory_source="frame")


def ops_kpi_data_fingerprint(database_url: str) -> str:
    """Cache-busting token when the dashboard reads from PostgreSQL (data + targets revision)."""
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*)::bigint, MAX(date) FROM ops_kpi_availability")
            count, max_date = cur.fetchone()
            tgt_sig = ""
            try:
                cur.execute(
                    "SELECT COALESCE(MAX(updated_at)::text, '') FROM ops_kpi_targets"
                )
                row = cur.fetchone()
                if row:
                    tgt_sig = row[0] or ""
            except Exception:
                pass
            cm_sig = ""
            try:
                cur.execute(
                    "SELECT COUNT(*)::bigint, COALESCE(MAX(event_timestamp)::text, '') FROM ops_kpi_cm"
                )
                row = cur.fetchone()
                if row:
                    cm_sig = f"{row[0] or 0}|{row[1] or ''}"
            except Exception:
                pass
    max_s = max_date.isoformat() if max_date is not None else ""
    return f"{count}|{max_s}|{tgt_sig}|{cm_sig}"


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
        return ""


def prepare_daily_availability_dataframe(
    data: pd.DataFrame,
    *,
    territory_source: Literal["excel", "frame"] = "excel",
) -> pd.DataFrame:
    """Normalize raw daily rows (ETL or database) into the dashboard frame."""
    missing_columns = sorted(CSV_REQUIRED_COLUMNS.difference(data.columns))
    if missing_columns:
        raise ValueError(
            "Data is missing required columns: " + ", ".join(missing_columns)
        )

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
        "Visit Count",
        "CM Count",
    ]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df["Region"] = (
        df["Region"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.upper()
        .replace({"METRO MANILA": "NCR"})
    )
    df = df[df["Region"].isin(REGION_ORDER)].copy()

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


def build_meta(
    df: pd.DataFrame, periods: dict[str, pd.Series], targets: OpsKpiTargets
) -> dict:
    return {
        "title": "Operations KPI Dashboard",
        "coverageText": (
            ""
        ),
        "periodOrder": [*periods.keys(), "TARGET"],
        "periodText": "",
        "limitations": [],
        "targetUi": {
            "baselineFactors": {
                "events": targets.events_baseline_factor,
                "cm": targets.cm_baseline_factor,
                "visit": targets.visit_baseline_factor,
            },
            "mttrMinutes": targets.mttr_minutes,
            "availabilityPct": targets.availability_pct,
        },
    }


def build_table_rows(
    df: pd.DataFrame, periods: dict[str, pd.Series], targets: OpsKpiTargets
) -> list[dict]:
    rows: list[dict] = []
    for region_index, region in enumerate(REGION_ORDER):
        region_df = scope_frame(df, region)
        zoo_names = ordered_zoo_names(region_df)
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
            )
        )
        for zoo_index, zoo_name in enumerate(zoo_names, start=1):
            zoo_df = region_df.loc[region_df["Zoo"] == zoo_name]
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
) -> dict:
    previous_fy_label = fiscal_year_labels(periods)[0]
    baseline_events = aggregate_event_count(scoped_df.loc[periods[previous_fy_label]])
    event_target = (
        baseline_events * targets.events_baseline_factor
        if baseline_events is not None
        else None
    )
    baseline_cm = aggregate_cm_count(scoped_df.loc[periods[previous_fy_label]])
    cm_target = (
        baseline_cm * targets.cm_baseline_factor if baseline_cm is not None else None
    )
    baseline_visit = aggregate_visit_count(scoped_df.loc[periods[previous_fy_label]])
    visit_target = (
        baseline_visit * targets.visit_baseline_factor
        if baseline_visit is not None
        else None
    )
    if event_target is not None:
        event_target = round(event_target)
    if cm_target is not None:
        cm_target = round(cm_target)
    if visit_target is not None:
        visit_target = round(visit_target)
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
            actuals=build_period_actuals(scoped_df, periods, aggregate_event_count),
            target=event_target,
            kind="number",
            compare_mode="upper_is_bad",
        ),
        "mttr": build_metric_group(
            actuals=build_period_actuals(scoped_df, periods, aggregate_mttr_minutes),
            target=targets.mttr_minutes,
            kind="number",
            compare_mode="upper_is_bad",
        ),
        "availability": build_metric_group(
            actuals=build_period_actuals(scoped_df, periods, aggregate_availability_pct),
            target=targets.availability_pct,
            kind="percent",
            compare_mode="lower_is_bad",
        ),
        "cm": build_metric_group(
            actuals=build_period_actuals(scoped_df, periods, aggregate_cm_count),
            target=cm_target,
            kind="number",
            compare_mode="upper_is_bad",
        ),
        "visit": build_metric_group(
            actuals=build_period_actuals(scoped_df, periods, aggregate_visit_count),
            target=visit_target,
            kind="number",
            compare_mode="upper_is_bad",
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
) -> dict:
    comparison_target = target
    built = {}
    for period, value in actuals.items():
        class_name = ""
        if value is not None and comparison_target is not None:
            if compare_mode == "upper_is_bad" and value > comparison_target:
                class_name = "text-warning"
            elif compare_mode == "lower_is_bad" and value < comparison_target:
                class_name = "text-danger"
        built[period] = build_value_cell(value, kind=kind, class_name=class_name)

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

    When loading from PostgreSQL, ``OPS_KPI_LOAD_SQL`` supplies ``site_table_site_id`` via
    ``public.site`` (same KPI-key mapping as CM events). Same row scope as other metrics:
    ``Region`` in ``REGION_ORDER``, ``Zoo != UNMAPPED_ZOO``, ``scope_frame`` for region rows.

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


def aggregate_event_count(df: pd.DataFrame) -> int | None:
    fact_df = _fact_rows_only(df)
    if fact_df.empty:
        return None
    return int(fact_df["Incident_count"].fillna(0).sum())


def aggregate_cm_count(df: pd.DataFrame) -> int | None:
    fact_df = _fact_rows_only(df)
    if fact_df.empty:
        return None
    return int(fact_df["CM Count"].fillna(0).sum())


def aggregate_visit_count(df: pd.DataFrame) -> int | None:
    fact_df = _fact_rows_only(df)
    if fact_df.empty:
        return None
    return int(fact_df["Visit Count"].fillna(0).sum())


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


def _chart_month_axes(df: pd.DataFrame) -> tuple[pd.PeriodIndex, list[str]]:
    end_period = df["month_period"].max()
    data_start = df["month_period"].min()
    start_period = max(data_start, CHART_MONTH_START)
    events_month_periods = pd.period_range(
        start=start_period, end=end_period, freq="M"
    )
    events_month_labels = [
        period.strftime("%b %Y") for period in events_month_periods.to_timestamp()
    ]
    return events_month_periods, events_month_labels


def _chart_bundle_for_scoped_df(
    scoped_df: pd.DataFrame,
    periods: dict[str, pd.Series],
    targets: OpsKpiTargets,
    events_month_periods: pd.PeriodIndex,
    events_month_labels: list[str],
) -> dict:
    event_target_series = build_monthly_target_series(
        scoped_df,
        periods,
        aggregate_event_count,
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
    visit_target_series = build_monthly_target_series(
        scoped_df,
        periods,
        aggregate_visit_count,
        events_month_periods,
        targets.visit_baseline_factor,
    )
    mt = targets.mttr_minutes
    av = targets.availability_pct
    return {
        "events": {
            "available": True,
            "months": events_month_labels,
            "actual": monthly_events(scoped_df, events_month_periods),
            "target": event_target_series,
        },
        "mttr": {
            "available": True,
            "months": events_month_labels,
            "actual": monthly_mttr(scoped_df, events_month_periods),
            "target": [mt] * len(events_month_periods),
        },
        "availability": {
            "available": True,
            "months": events_month_labels,
            "actual": monthly_availability(scoped_df, events_month_periods),
            "target": [av] * len(events_month_periods),
        },
        "cm": {
            "available": True,
            "months": events_month_labels,
            "actual": monthly_cm_count(scoped_df, events_month_periods),
            "target": cm_target_series,
        },
        "visit": {
            "available": True,
            "months": events_month_labels,
            "actual": monthly_visit_count(scoped_df, events_month_periods),
            "target": visit_target_series,
        },
    }


def build_charts(
    df: pd.DataFrame, periods: dict[str, pd.Series], targets: OpsKpiTargets
) -> dict:
    events_month_periods, events_month_labels = _chart_month_axes(df)
    charts = {}
    for scope in CHART_ROW_ORDER:
        scoped_df = scope_frame(df, scope)
        charts[scope] = _chart_bundle_for_scoped_df(
            scoped_df,
            periods,
            targets,
            events_month_periods,
            events_month_labels,
        )
    return charts


def build_territory_charts(
    df: pd.DataFrame, periods: dict[str, pd.Series], targets: OpsKpiTargets
) -> tuple[list[str], dict[str, dict]]:
    events_month_periods, events_month_labels = _chart_month_axes(df)
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
        )
    return territory_order, charts


def monthly_events(df: pd.DataFrame, month_periods: pd.PeriodIndex) -> list[int | None]:
    values: list[int | None] = []
    for period in month_periods:
        value = aggregate_event_count(df.loc[df["month_period"] == period])
        values.append(int(value) if value is not None else None)
    return values


def monthly_cm_count(df: pd.DataFrame, month_periods: pd.PeriodIndex) -> list[int | None]:
    values: list[int | None] = []
    for period in month_periods:
        value = aggregate_cm_count(df.loc[df["month_period"] == period])
        values.append(int(value) if value is not None else None)
    return values


def monthly_visit_count(df: pd.DataFrame, month_periods: pd.PeriodIndex) -> list[int | None]:
    values: list[int | None] = []
    for period in month_periods:
        value = aggregate_visit_count(df.loc[df["month_period"] == period])
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
