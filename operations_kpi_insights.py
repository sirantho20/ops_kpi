from __future__ import annotations

import io
import logging
import math
import re
from typing import Any

logger = logging.getLogger("operations_kpi.insights")

import pandas as pd
import psycopg

from operations_kpi_data import (
    CSV_REQUIRED_COLUMNS,
    OpsKpiTargets,
    aggregate_availability_pct,
    aggregate_cm_count,
    aggregate_event_count_table,
    aggregate_mttr_minutes_table,
    aggregate_site_visit_count_table,
    aggregate_visit_count_table,
    availability_pct_for_region_scope,
    build_visit_compact_periods,
    fetch_ops_kpi_metrics_for_date_range,
    fiscal_year_labels,
    format_value,
    period_date_range_for_insight,
    scope_frame,
)

METRICS = frozenset({"events", "mttr", "availability", "cm", "visit", "siteVisit"})
ROW_KINDS = frozenset({"region", "zoo", "footer"})
TOP_N = 15

_METRIC_COMPARE = {
    "events": "upper_is_bad",
    "mttr": "upper_is_bad",
    "availability": "lower_is_bad",
    "cm": "upper_is_bad",
    "visit": "upper_is_bad",
    "siteVisit": "upper_is_bad",
}

_EXPORT_DERIVED_COLUMNS = (
    "ptci_site_id",
    "availability_weight",
    "availability_ratio",
    "availability_fallback_ratio",
    "month_period",
)

_FILENAME_UNSAFE = re.compile(r"[^\w.\-]+")


def resolve_scoped_df(
    df: pd.DataFrame, row_kind: str, region: str, zoo: str | None
) -> pd.DataFrame:
    if row_kind == "footer":
        return scope_frame(df, "Overall")
    if row_kind == "region":
        return scope_frame(df, region)
    if row_kind == "zoo":
        if not zoo:
            raise ValueError("zoo is required when row_kind is zoo")
        return df.loc[(df["Region"] == region) & (df["Zoo"] == zoo)].copy()
    raise ValueError(f"Invalid row_kind: {row_kind}")


def _pla_ptci_for_ptci_site_id(df: pd.DataFrame, ptci_site_id: str) -> tuple[str, str]:
    sub = df.loc[df["ptci_site_id"].astype(str) == str(ptci_site_id)]
    if sub.empty:
        return "", ""
    pla = sub["PLA ID"].dropna()
    ptci = sub["PTCI Number"].dropna()
    pla_s = str(pla.iloc[0]) if len(pla) else ""
    ptci_s = str(ptci.iloc[0]) if len(ptci) else ""
    return pla_s, ptci_s


def _json_float(x: float | int | None) -> float | int | None:
    if x is None:
        return None
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    if isinstance(x, (float, int)):
        return float(x) if isinstance(x, float) else int(x)
    return x


def _meets_target(
    actual: float | int | None,
    target: float | int | None,
    compare_mode: str,
) -> bool | None:
    if actual is None or target is None:
        return None
    if compare_mode == "upper_is_bad":
        return actual <= target
    if compare_mode == "lower_is_bad":
        return actual >= target
    return None


def _row_targets(
    scoped_df: pd.DataFrame,
    periods: dict[str, pd.Series],
    ops_targets: OpsKpiTargets,
    *,
    row_kind: str,
    region: str | None,
) -> dict[str, float | int | None]:
    previous_fy_label = fiscal_year_labels(periods)[0]
    mask = periods[previous_fy_label]
    baseline_events = aggregate_event_count_table(scoped_df.loc[mask])
    baseline_cm = aggregate_cm_count(scoped_df.loc[mask])
    baseline_visit = aggregate_visit_count_table(scoped_df.loc[mask])
    baseline_site_visit = aggregate_site_visit_count_table(scoped_df.loc[mask])
    if row_kind == "footer":
        availability_target = ops_targets.availability_pct
    else:
        availability_target = availability_pct_for_region_scope(region or "Overall", ops_targets)
    return {
        "events": (
            baseline_events * ops_targets.events_baseline_factor
            if baseline_events is not None
            else None
        ),
        "mttr": ops_targets.mttr_minutes,
        "availability": availability_target,
        "cm": (
            baseline_cm * ops_targets.cm_baseline_factor
            if baseline_cm is not None
            else None
        ),
        "visit": (
            baseline_visit * ops_targets.visit_baseline_factor
            if baseline_visit is not None
            else None
        ),
        "siteVisit": (
            baseline_site_visit * ops_targets.visit_baseline_factor
            if baseline_site_visit is not None
            else None
        ),
    }


def _target_explanation(
    metric: str,
    scoped_df: pd.DataFrame,
    periods: dict[str, pd.Series],
    targets: dict[str, float | int | None],
    ops_targets: OpsKpiTargets,
) -> str:
    prev_fy, _ = fiscal_year_labels(periods)
    prev_mask = periods[prev_fy]
    def baseline_text(v: float | int | None) -> str:
        return format_value(v, kind="number")

    if metric == "events":
        bf = ops_targets.events_baseline_factor
        b = aggregate_event_count_table(scoped_df.loc[prev_mask])
        t = targets["events"]
        return (
            f"Target is {bf:.0%} of {prev_fy} total incidents. "
            f"Baseline ({prev_fy}): {baseline_text(b)} incidents → target: {format_value(t, kind='number')}."
        )
    if metric == "cm":
        bf = ops_targets.cm_baseline_factor
        b = aggregate_cm_count(scoped_df.loc[prev_mask])
        t = targets["cm"]
        return (
            f"Target is {bf:.0%} of {prev_fy} CM count. "
            f"Baseline ({prev_fy}): {baseline_text(b)} → target: {format_value(t, kind='number')}."
        )
    if metric == "visit":
        bf = ops_targets.visit_baseline_factor
        b = aggregate_visit_count_table(scoped_df.loc[prev_mask])
        t = targets["visit"]
        return (
            f"Target is {bf:.0%} of {prev_fy} SIC count. "
            f"Baseline ({prev_fy}): {baseline_text(b)} → target: {format_value(t, kind='number')}."
        )
    if metric == "siteVisit":
        bf = ops_targets.visit_baseline_factor
        b = aggregate_site_visit_count_table(scoped_df.loc[prev_mask])
        t = targets["siteVisit"]
        return (
            f"Target is {bf:.0%} of {prev_fy} site visit count. "
            f"Baseline ({prev_fy}): {baseline_text(b)} → target: {format_value(t, kind='number')}."
        )
    if metric == "mttr":
        return (
            f"Target is mean accepted outage minutes below {ops_targets.mttr_minutes:.0f} minutes "
            "within the scoped rows."
        )
    if metric == "availability":
        t = targets["availability"]
        pct = float(t) if t is not None else ops_targets.availability_pct
        return (
            f"Target is weighted availability at or above {pct:.2f}% "
            "(same weighting as the main table: minutes-weighted ratio, with uptime fallback)."
        )
    return ""


def _sum_metric_breakdown(
    period_df: pd.DataFrame, column: str
) -> tuple[list[dict[str, Any]], int]:
    if period_df.empty:
        return [], 0
    sums = (
        period_df.groupby("ptci_site_id", dropna=True)[column]
        .sum()
        .sort_values(ascending=False)
    )
    total = int(sums.sum())
    out: list[dict[str, Any]] = []
    for pid, val in sums.head(TOP_N).items():
        pla, ptci = _pla_ptci_for_ptci_site_id(period_df, str(pid))
        v = int(val)
        pct = (100.0 * v / total) if total else 0.0
        out.append(
            {
                "site_key": str(pid),
                "pla_id": pla,
                "ptci": ptci,
                "value": v,
                "pct_of_total": round(pct, 2),
            }
        )
    top_sum = int(sums.head(TOP_N).sum())
    other = total - top_sum
    return out, other


def _availability_site_scores(period_df: pd.DataFrame) -> list[tuple[str, float, float]]:
    rows: list[tuple[str, float, float]] = []
    for pid, g in period_df.groupby("ptci_site_id", dropna=True):
        pct = aggregate_availability_pct(g)
        if pct is None:
            continue
        w = float(pd.to_numeric(g["availability_weight"], errors="coerce").fillna(0).sum())
        rows.append((str(pid), float(pct), w))
    rows.sort(key=lambda x: x[1])
    return rows


def _validate_metric_period(
    metric: str,
    period_key: str,
    periods: dict[str, pd.Series],
    visit_compact: dict[str, pd.Series],
) -> None:
    if metric not in METRICS:
        msg = f"Invalid metric: {metric}"
        logger.warning(msg)
        raise ValueError(msg)
    period_ok = (
        period_key == "TARGET"
        or period_key in periods
        or (metric in ("visit", "siteVisit") and period_key in visit_compact)
    )
    if not period_ok:
        msg = f"Invalid period: {period_key}"
        logger.warning("%s (metric=%s)", msg, metric)
        raise ValueError(msg)


def resolve_cell_period_df(
    df: pd.DataFrame,
    periods: dict[str, pd.Series],
    row_kind: str,
    region: str,
    zoo: str | None,
    metric: str,
    period_key: str,
) -> pd.DataFrame:
    """Scoped daily/site rows for the clicked cell (empty when period is TARGET)."""
    visit_compact = build_visit_compact_periods(df)
    _validate_metric_period(metric, period_key, periods, visit_compact)
    if period_key == "TARGET":
        return pd.DataFrame()
    scoped = resolve_scoped_df(df, row_kind, region, zoo)
    if period_key in periods:
        period_mask = periods[period_key]
    else:
        period_mask = visit_compact[period_key]
    return scoped.loc[period_mask].copy()


def _export_columns(period_df: pd.DataFrame) -> list[str]:
    ordered: list[str] = []
    for col in CSV_REQUIRED_COLUMNS:
        if col in period_df.columns:
            ordered.append(col)
    for col in _EXPORT_DERIVED_COLUMNS:
        if col in period_df.columns and col not in ordered:
            ordered.append(col)
    return ordered


def _slug_filename_part(value: str) -> str:
    return _FILENAME_UNSAFE.sub("_", value.strip()) or "unknown"


def cell_insight_export_filename(
    *,
    metric: str,
    region: str,
    period_key: str,
    zoo: str | None = None,
) -> str:
    parts = ["ops_kpi", _slug_filename_part(metric), _slug_filename_part(region)]
    if zoo:
        parts.append(_slug_filename_part(zoo))
    parts.append(_slug_filename_part(period_key))
    return "_".join(parts) + ".csv"


def build_cell_insight_csv(
    df: pd.DataFrame,
    periods: dict[str, pd.Series],
    row_kind: str,
    region: str,
    zoo: str | None,
    metric: str,
    period_key: str,
) -> tuple[bytes, str]:
    if period_key == "TARGET":
        msg = "TARGET cells have no underlying period data to export"
        logger.warning(msg)
        raise ValueError(msg)
    period_df = resolve_cell_period_df(
        df, periods, row_kind, region, zoo, metric, period_key
    )
    columns = _export_columns(period_df)
    export_df = period_df[columns].copy() if columns else period_df.copy()
    if "Date" in export_df.columns:
        export_df["Date"] = pd.to_datetime(export_df["Date"], errors="coerce").dt.strftime(
            "%Y-%m-%d"
        )
    if "month_period" in export_df.columns:
        export_df["month_period"] = export_df["month_period"].astype(str)
    buf = io.StringIO()
    export_df.to_csv(buf, index=False)
    filename = cell_insight_export_filename(
        metric=metric,
        region=region,
        period_key=period_key,
        zoo=zoo,
    )
    logger.info(
        "cell insight CSV export: filename=%s rows=%d",
        filename,
        len(export_df),
    )
    return buf.getvalue().encode("utf-8"), filename


def _mttr_site_scores(period_df: pd.DataFrame) -> list[tuple[str, float, int]]:
    rows: list[tuple[str, float, int]] = []
    for pid, g in period_df.groupby("ptci_site_id", dropna=True):
        sub = g.loc[g["Accepted Outage Minutes"] > 0, "Accepted Outage Minutes"].dropna()
        if sub.empty:
            continue
        m = float(sub.mean())
        n = int(len(sub))
        rows.append((str(pid), m, n))
    rows.sort(key=lambda x: -x[1])
    return rows


def compute_cell_insight(
    df: pd.DataFrame,
    periods: dict[str, pd.Series],
    row_kind: str,
    region: str,
    zoo: str | None,
    metric: str,
    period_key: str,
    ops_targets: OpsKpiTargets,
    *,
    database_url: str | None = None,
) -> dict[str, Any]:
    logger.info(
        "compute_cell_insight: row_kind=%s region=%r metric=%s period=%s zoo=%r",
        row_kind,
        region,
        metric,
        period_key,
        zoo,
    )
    visit_compact = build_visit_compact_periods(df)
    _validate_metric_period(metric, period_key, periods, visit_compact)

    scoped = resolve_scoped_df(df, row_kind, region, zoo)
    logger.debug("compute_cell_insight: scoped_rows=%d", len(scoped))
    targets = _row_targets(
        scoped, periods, ops_targets, row_kind=row_kind, region=region
    )
    compare_mode = _METRIC_COMPARE[metric]

    label_parts = [region or ""]
    if zoo:
        label_parts.append(zoo)
    scope_label = " / ".join(p for p in label_parts if p)

    result: dict[str, Any] = {
        "metric": metric,
        "period": period_key,
        "rowKind": row_kind,
        "region": region,
        "zoo": zoo or "",
        "scope_label": scope_label,
        "compare_mode": compare_mode,
    }

    if period_key == "TARGET":
        t = targets[metric]
        result["actual"] = None
        result["target"] = _json_float(t) if t is not None else None
        result["meets_target"] = None
        result["summary"] = "This cell shows the performance target for the selected row scope."
        result["target_explanation"] = _target_explanation(
            metric, scoped, periods, targets, ops_targets
        )
        return result

    period_df = resolve_cell_period_df(
        df, periods, row_kind, region, zoo, metric, period_key
    )
    logger.debug("compute_cell_insight: period_rows=%d", len(period_df))

    sql_triple: tuple[int, float | None, float | None] | None = None
    if database_url and metric in ("events", "mttr", "availability"):
        d0, d1 = period_date_range_for_insight(
            df, periods, period_key, extra_periods=visit_compact
        )
        if d0 is not None and d1 is not None:
            with psycopg.connect(database_url) as conn:
                with conn.cursor() as cur:
                    sql_triple = fetch_ops_kpi_metrics_for_date_range(
                        cur,
                        d0,
                        d1,
                        row_kind=row_kind,
                        region=region if row_kind in ("region", "zoo") else None,
                        zoo=zoo if row_kind == "zoo" else None,
                    )

    if metric == "events":
        actual = (
            sql_triple[0]
            if sql_triple is not None
            else aggregate_event_count_table(period_df)
        )
        target = targets["events"]
    elif metric == "cm":
        actual = aggregate_cm_count(period_df)
        target = targets["cm"]
    elif metric == "visit":
        actual = aggregate_visit_count_table(period_df)
        target = targets["visit"]
    elif metric == "siteVisit":
        actual = aggregate_site_visit_count_table(period_df)
        target = targets["siteVisit"]
    elif metric == "mttr":
        actual = (
            sql_triple[1]
            if sql_triple is not None
            else aggregate_mttr_minutes_table(period_df)
        )
        target = targets["mttr"]
    else:
        actual = (
            sql_triple[2]
            if sql_triple is not None
            else aggregate_availability_pct(period_df)
        )
        target = targets["availability"]

    meets = _meets_target(actual, target, compare_mode)

    result["actual"] = _json_float(actual)
    result["target"] = _json_float(target)
    result["meets_target"] = meets

    # Summary line
    kind = "percent" if metric == "availability" else "number"
    av = format_value(actual, kind=kind)
    tv = format_value(target, kind=kind) if target is not None else "N/A"
    status = (
        "meets target"
        if meets is True
        else ("below target" if meets is False else "n/a")
    )
    result["summary"] = f"Actual {av} vs target {tv} ({status}) for {period_key} — {scope_label}."

    if metric in ("events", "cm", "visit", "siteVisit"):
        col = {
            "events": "Incident_count",
            "cm": "CM Count",
            "visit": "SIC Count",
            "siteVisit": "Site Visit Count",
        }[metric]
        top, other = _sum_metric_breakdown(period_df, col)
        result["top_contributors"] = top
        if other > 0:
            result["other_contributors_total"] = other

    elif metric == "mttr":
        scores = _mttr_site_scores(period_df)[:TOP_N]
        result["highest_mttr_sites"] = []
        for sk, m, n in scores:
            pla, ptci = _pla_ptci_for_ptci_site_id(period_df, sk)
            result["highest_mttr_sites"].append(
                {
                    "site_key": sk,
                    "pla_id": pla,
                    "ptci": ptci,
                    "mean_minutes": round(m, 2),
                    "outage_rows": n,
                }
            )

    elif metric == "availability":
        scores = _availability_site_scores(period_df)
        worst = scores[:TOP_N]
        result["worst_sites"] = []
        for sk, pct, w in worst:
            pla, ptci = _pla_ptci_for_ptci_site_id(period_df, sk)
            result["worst_sites"].append(
                {
                    "site_key": sk,
                    "pla_id": pla,
                    "ptci": ptci,
                    "availability_pct": round(pct, 4),
                    "weighted_minutes": round(w, 2),
                }
            )
        if meets is False and target is not None:
            result["below_target_note"] = (
                f"Sites listed are the lowest availability (weighted) within this cell; "
                f"target is {target:.2f}%."
            )

    return result
