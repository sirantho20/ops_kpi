#!/usr/bin/env python3
"""
Phase 1: Transpose the Overall workbook (monthly tabs), then optional supplementary
workbooks (e.g. Feb/March weekly outage files) using sheet 'Daily Site Availability';
harmonize schema; concat chronologically.
Phase 2: Merge with legacy daily_availability_transformed.csv, dedupe, write xlsx/csv.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from transform_daily_availability_robust import transform_daily_availability

LEGACY_COLUMNS = [
    "PTCI Number",
    "PLA ID",
    "Domain",
    "Portfolio",
    "Anchor/Colocation",
    "Region",
    "Province",
    "Sub Region",
    "Status",
    "Globe",
    "Smart",
    "DITO",
    "No. OF Tenant",
    "Total Incident count",
    "Total Available Minutes",
    "Accepted Outage Minutes",
    "Availability",
    "Date",
    "Incident_count",
    "Outage_mins",
    "Uptime_per_tenant",
    "Visit Count",
    "CM Count",
    "Zoo",
]

MONTH_PARSE_FORMATS = ("%B %Y", "%b %Y")

# Inclusive reporting window for auto-discovered tabs (year, month)
WINDOW_START = (2025, 7)
WINDOW_END = (2026, 1)

DAILY_SITE_AVAILABILITY_SHEET = "Daily Site Availability"
DISPATCH_SHEET_NAME = "in"
DISPATCH_REQUIRED_COLUMNS = {"Site ID", "Problem Start Date: Day", "NV", "CM"}
ZOO_MAPPING_REQUIRED_COLUMNS = {"Site ID", "Zoo"}


def _month_ge(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] > b[0] or (a[0] == b[0] and a[1] >= b[1])


def _month_le(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[0] or (a[0] == b[0] and a[1] <= b[1])


def parse_tab_month(tab_name: str) -> datetime | None:
    s = tab_name.strip()
    for fmt in MONTH_PARSE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def monthly_tabs_to_process(
    xl_path: str | Path,
    tabs_override: list[str] | None = None,
) -> list[tuple[str, datetime]]:
    xl_path = Path(xl_path)
    xf = pd.ExcelFile(xl_path)
    print(f"Workbook sheets ({len(xf.sheet_names)}): {xf.sheet_names}")

    if tabs_override:
        ordered: list[tuple[str, datetime]] = []
        for tab in tabs_override:
            if tab not in xf.sheet_names:
                raise ValueError(f"Sheet {tab!r} not in workbook")
            dt = parse_tab_month(tab)
            if dt is None:
                raise ValueError(f"Could not parse month from tab name {tab!r}")
            ordered.append((tab, dt))
        ordered.sort(key=lambda x: (x[1].year, x[1].month))
        return ordered

    picked: list[tuple[str, datetime]] = []
    for tab in xf.sheet_names:
        dt = parse_tab_month(tab)
        if dt is None:
            continue
        ym = (dt.year, dt.month)
        if _month_ge(ym, WINDOW_START) and _month_le(ym, WINDOW_END):
            picked.append((tab, dt))

    picked.sort(key=lambda x: (x[1].year, x[1].month))
    if not picked:
        raise ValueError(
            f"No monthly tabs found in range {WINDOW_START}–{WINDOW_END}. "
            "Use --tabs to pass explicit sheet names."
        )
    return picked


def harmonize_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.rename(
        columns={
            "Globe ID": "Globe",
            "Smart ID": "Smart",
            "DITO ID": "DITO",
            "New ID": "Domain",
            "#. OF Tenant": "No. OF Tenant",
        }
    )
    for c in ("Total Available Minutes", "Accepted Outage Minutes", "Availability"):
        if c not in out.columns:
            out[c] = np.nan
    out = out.reindex(columns=list(out.columns.union(LEGACY_COLUMNS)))
    out = out.reindex(columns=LEGACY_COLUMNS)
    return out


def normalize_identifier_series(series: pd.Series) -> pd.Series:
    cleaned = series.astype(str).str.strip()
    cleaned = cleaned.replace(
        {"": pd.NA, "-": pd.NA, "nan": pd.NA, "None": pd.NA, "<NA>": pd.NA}
    )
    return cleaned


def load_dispatch_metrics(
    dispatch_path: str | Path, sheet_name: str = DISPATCH_SHEET_NAME
) -> pd.DataFrame:
    dispatch_path = Path(dispatch_path)
    if not dispatch_path.is_file():
        raise FileNotFoundError(f"Dispatch workbook not found: {dispatch_path}")

    dispatch = pd.read_excel(dispatch_path, sheet_name=sheet_name)
    missing = sorted(DISPATCH_REQUIRED_COLUMNS.difference(dispatch.columns))
    if missing:
        raise ValueError(
            "Dispatch workbook is missing required columns: " + ", ".join(missing)
        )

    data = dispatch.loc[:, ["Site ID", "Problem Start Date: Day", "NV", "CM"]].copy()
    data["Site ID"] = normalize_identifier_series(data["Site ID"])
    data["Date"] = pd.to_datetime(data["Problem Start Date: Day"], errors="coerce").dt.normalize()
    data["Visit Count"] = pd.to_numeric(data["NV"], errors="coerce")
    data["CM Count"] = pd.to_numeric(data["CM"], errors="coerce")
    data = data.drop(columns=["Problem Start Date: Day", "NV", "CM"])
    data = data.dropna(subset=["Site ID", "Date"])
    data = data.loc[data[["Visit Count", "CM Count"]].notna().any(axis=1)].copy()

    grouped = (
        data.groupby(["Site ID", "Date"], dropna=False)[["Visit Count", "CM Count"]]
        .sum(min_count=1)
        .reset_index()
    )
    grouped["Visit Count"] = grouped["Visit Count"].fillna(0).astype(int)
    grouped["CM Count"] = grouped["CM Count"].fillna(0).astype(int)
    return grouped


def merge_dispatch_metrics(
    availability_df: pd.DataFrame, dispatch_path: str | Path
) -> pd.DataFrame:
    merged = availability_df.copy()
    merged["Date"] = pd.to_datetime(merged["Date"], errors="coerce").dt.normalize()
    merged["_dispatch_site_id"] = normalize_identifier_series(merged["PTCI Number"])

    for column in ("Visit Count", "CM Count"):
        if column in merged.columns:
            merged = merged.drop(columns=[column])

    dispatch = load_dispatch_metrics(dispatch_path)
    merged = merged.merge(
        dispatch,
        how="left",
        left_on=["_dispatch_site_id", "Date"],
        right_on=["Site ID", "Date"],
    )
    merged = merged.drop(columns=["_dispatch_site_id", "Site ID"])
    merged["Visit Count"] = pd.to_numeric(merged["Visit Count"], errors="coerce").fillna(0).astype(int)
    merged["CM Count"] = pd.to_numeric(merged["CM Count"], errors="coerce").fillna(0).astype(int)
    return merged


def load_zoo_mapping(mapping_path: str | Path) -> pd.DataFrame:
    mapping_path = Path(mapping_path)
    if not mapping_path.is_file():
        raise FileNotFoundError(f"Zoo mapping CSV not found: {mapping_path}")

    mapping = pd.read_csv(mapping_path)
    missing = sorted(ZOO_MAPPING_REQUIRED_COLUMNS.difference(mapping.columns))
    if missing:
        raise ValueError(
            "Zoo mapping CSV is missing required columns: " + ", ".join(missing)
        )

    cleaned = mapping.loc[:, ["Site ID", "Zoo"]].copy()
    cleaned["Site ID"] = normalize_identifier_series(cleaned["Site ID"])
    cleaned["Zoo"] = (
        cleaned["Zoo"]
        .astype(str)
        .str.strip()
        .replace({"": pd.NA, "nan": pd.NA, "None": pd.NA, "<NA>": pd.NA})
    )
    cleaned = cleaned.dropna(subset=["Site ID", "Zoo"]).copy()

    conflicts = cleaned.groupby("Site ID")["Zoo"].nunique()
    conflicting_ids = conflicts[conflicts > 1].index.tolist()
    if conflicting_ids:
        sample = ", ".join(conflicting_ids[:10])
        raise ValueError(
            "Zoo mapping contains conflicting Zoo assignments for Site ID values: "
            f"{sample}"
        )

    cleaned = cleaned.drop_duplicates(subset=["Site ID"], keep="first")
    return cleaned.reset_index(drop=True)


def merge_zoo_mapping(
    availability_df: pd.DataFrame, mapping_path: str | Path
) -> pd.DataFrame:
    merged = availability_df.copy()
    merged["_zoo_site_id"] = normalize_identifier_series(merged["PTCI Number"])
    if "Zoo" in merged.columns:
        merged = merged.drop(columns=["Zoo"])

    mapping = load_zoo_mapping(mapping_path)
    merged = merged.merge(
        mapping,
        how="left",
        left_on="_zoo_site_id",
        right_on="Site ID",
    )
    merged = merged.drop(columns=["_zoo_site_id", "Site ID"])
    return merged


def filter_rows_to_tab_month(
    df: pd.DataFrame, tab: str, tab_dt: datetime
) -> pd.DataFrame:
    """Keep only rows whose Date falls in the tab's calendar month (drops stray triplet columns)."""
    if df.empty:
        raise ValueError(f"Tab {tab!r}: empty after transpose")
    dates = pd.to_datetime(df["Date"], errors="coerce")
    if dates.isna().any():
        raise ValueError(f"Tab {tab!r}: invalid Date values present")
    exp_y, exp_m = tab_dt.year, tab_dt.month
    mask = (dates.dt.year == exp_y) & (dates.dt.month == exp_m)
    n_drop = int((~mask).sum())
    if n_drop:
        extras = sorted(dates[~mask].dt.date.unique().tolist())
        print(
            f"WARNING: tab {tab!r}: dropping {n_drop} row(s) with Date outside "
            f"{exp_y}-{exp_m:02d} (e.g. extra columns on sheet): {extras[:8]}{'...' if len(extras) > 8 else ''}"
        )
    out = df.loc[mask].copy()
    out["Date"] = pd.to_datetime(out["Date"]).dt.normalize()
    return out.reset_index(drop=True)


def transpose_all_sheets(
    xl_path: str | Path,
    tabs_override: list[str] | None = None,
) -> pd.DataFrame:
    xl_path = Path(xl_path)
    tabs_meta = monthly_tabs_to_process(xl_path, tabs_override=tabs_override)
    print("Tabs to process (chronological):")
    for tab, dt in tabs_meta:
        print(f"  - {tab!r} -> {dt:%Y-%m}")

    frames: list[pd.DataFrame] = []
    for tab, tab_dt in tabs_meta:
        print(f"\n--- Transposing {tab!r} ---")
        raw = transform_daily_availability(
            str(xl_path),
            sheet_name=tab,
            header_row=2,
            data_start_row=3,
        )
        raw = filter_rows_to_tab_month(raw, tab, tab_dt)
        frames.append(harmonize_columns(raw))

    overall = pd.concat(frames, ignore_index=True)
    overall["Date"] = pd.to_datetime(overall["Date"]).dt.normalize()
    print(
        f"\nOverall workbook Phase 1: {len(overall)} rows, "
        f"Date {overall['Date'].min().date()} .. {overall['Date'].max().date()}"
    )
    return overall


def transpose_supplementary_workbooks(
    entries: list[tuple[Path, datetime, str]],
) -> pd.DataFrame:
    """
    Transpose each supplementary xlsx (same wide layout), filter to the given calendar month.

    entries: list of (path, first_day_of_month_dt, log_label)
    """
    frames: list[pd.DataFrame] = []
    for path, tab_dt, label in entries:
        if not path.is_file():
            raise FileNotFoundError(f"Supplementary workbook not found: {path}")
        print(f"\n--- Supplementary {label!r} ({path.name}) -> {tab_dt:%Y-%m} ---")
        raw = transform_daily_availability(
            str(path),
            sheet_name=DAILY_SITE_AVAILABILITY_SHEET,
            header_row=2,
            data_start_row=3,
        )
        raw = filter_rows_to_tab_month(raw, label, tab_dt)
        frames.append(harmonize_columns(raw))

    out = pd.concat(frames, ignore_index=True)
    out["Date"] = pd.to_datetime(out["Date"]).dt.normalize()
    print(
        f"\nSupplementary workbooks: {len(out)} rows, "
        f"Date {out['Date'].min().date()} .. {out['Date'].max().date()}"
    )
    return out


def phase1_combined(
    overall_xl: Path,
    tabs_override: list[str] | None,
    supplementary_entries: list[tuple[Path, datetime, str]],
) -> pd.DataFrame:
    overall = transpose_all_sheets(overall_xl, tabs_override=tabs_override)
    if not supplementary_entries:
        return overall
    weekly = transpose_supplementary_workbooks(supplementary_entries)
    combined = pd.concat([overall, weekly], ignore_index=True)
    combined["Date"] = pd.to_datetime(combined["Date"]).dt.normalize()
    print(
        f"\nPhase 1 combined: {len(combined)} rows, "
        f"Date {combined['Date'].min().date()} .. {combined['Date'].max().date()}"
    )
    return combined


def normalize_for_merge(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["Date"] = pd.to_datetime(out["Date"], errors="coerce").dt.normalize()
    for col in ("PLA ID", "PTCI Number"):
        if col not in out.columns:
            continue
        out[col] = normalize_identifier_series(out[col])
    return out


def dedupe_key_series(df: pd.DataFrame) -> pd.Series:
    pla = df["PLA ID"]
    ptci = df["PTCI Number"]
    use_ptci = pla.isna() | (pla.astype(str).str.strip() == "")
    key_site = np.where(use_ptci, ptci.astype(str).str.strip(), pla.astype(str).str.strip())
    return pd.Series(key_site, index=df.index).astype(str) + "|" + df["Date"].astype(str)


def merge_with_legacy(
    overall_from_workbook: pd.DataFrame,
    legacy_path: str | Path,
) -> tuple[pd.DataFrame, int]:
    legacy_path = Path(legacy_path)
    legacy = pd.read_csv(legacy_path)
    legacy = legacy.reindex(columns=LEGACY_COLUMNS)
    wb = overall_from_workbook.reindex(columns=LEGACY_COLUMNS)

    legacy_n = normalize_for_merge(legacy)
    wb_n = normalize_for_merge(wb)

    legacy_n["_src"] = 0
    wb_n["_src"] = 1
    merged = pd.concat([legacy_n, wb_n], ignore_index=True)
    merged["_dedupe_key"] = dedupe_key_series(merged)

    before = len(merged)
    merged = merged.sort_values(["_dedupe_key", "_src"], kind="mergesort")
    merged = merged.drop_duplicates(subset=["_dedupe_key"], keep="last")
    dropped = before - len(merged)

    merged = merged.drop(columns=["_dedupe_key", "_src"])
    merged = merged.sort_values(["Date", "PLA ID", "PTCI Number"], kind="mergesort")
    merged = merged.reset_index(drop=True)
    return merged, dropped


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--workbook",
        default="Ovearll Site Availabilty.xlsx",
        help="Path to Overall Site Availability xlsx",
    )
    p.add_argument(
        "--legacy",
        default="daily_availability_transformed.csv",
        help="Legacy transformed CSV to merge",
    )
    p.add_argument(
        "--output-xlsx",
        default="daily_availability_transformed.xlsx",
        help="Merged output Excel path",
    )
    p.add_argument(
        "--output-csv",
        default="daily_availability_transformed.csv",
        help="Refresh legacy CSV with merged data (same path as default legacy)",
    )
    p.add_argument(
        "--no-csv",
        action="store_true",
        help="Do not write CSV (only xlsx)",
    )
    p.add_argument(
        "--tabs",
        default="",
        help="Comma-separated sheet names to process (override auto-discovery)",
    )
    p.add_argument(
        "--feb-weekly",
        default="Feb_2026_Weekly_Outage.xlsx",
        help="Feb 2026 weekly outage workbook (Daily Site Availability sheet)",
    )
    p.add_argument(
        "--mar-weekly",
        default="March_2026_Weekly_Outage.xlsx",
        help="March 2026 weekly outage workbook (Daily Site Availability sheet)",
    )
    p.add_argument(
        "--dispatch-workbook",
        default="CM Dispatch Daily Distribution V2.xlsx",
        help="Path to CM dispatch workbook with Site ID, date, NV, and CM counts.",
    )
    p.add_argument(
        "--zoo-mapping",
        default="site_site.csv",
        help="Path to Zoo mapping CSV used to enrich the consolidated master data.",
    )
    args = p.parse_args()

    root = Path(__file__).resolve().parent
    wb_path = root / args.workbook if not Path(args.workbook).is_absolute() else Path(args.workbook)
    legacy_path = root / args.legacy if not Path(args.legacy).is_absolute() else Path(args.legacy)
    out_xlsx = root / args.output_xlsx if not Path(args.output_xlsx).is_absolute() else Path(args.output_xlsx)
    dispatch_path = (
        root / args.dispatch_workbook
        if not Path(args.dispatch_workbook).is_absolute()
        else Path(args.dispatch_workbook)
    )
    zoo_mapping_path = (
        root / args.zoo_mapping
        if not Path(args.zoo_mapping).is_absolute()
        else Path(args.zoo_mapping)
    )

    tabs_override = [t.strip() for t in args.tabs.split(",") if t.strip()] or None

    feb_p = root / args.feb_weekly if not Path(args.feb_weekly).is_absolute() else Path(args.feb_weekly)
    mar_p = root / args.mar_weekly if not Path(args.mar_weekly).is_absolute() else Path(args.mar_weekly)
    supplementary = [
        (feb_p, datetime(2026, 2, 1), "Feb_2026_Weekly_Outage"),
        (mar_p, datetime(2026, 3, 1), "March_2026_Weekly_Outage"),
    ]

    combined = phase1_combined(wb_path, tabs_override, supplementary)
    merged, n_dup = merge_with_legacy(combined, legacy_path)
    merged = merge_dispatch_metrics(merged, dispatch_path)
    merged = merge_zoo_mapping(merged, zoo_mapping_path)

    print(f"\nPhase 2: removed {n_dup} duplicate key rows (PLA ID|PTCI Number + Date)")
    print(
        f"Merged shape: {len(merged)} rows, "
        f"Date {merged['Date'].min().date()} .. {merged['Date'].max().date()}"
    )
    print(
        "Dispatch totals merged into output: "
        f"Visit Count={int(merged['Visit Count'].sum()):,}, "
        f"CM Count={int(merged['CM Count'].sum()):,}"
    )
    mapped_zoo_sites = int(merged.loc[merged["Zoo"].notna(), "PTCI Number"].nunique())
    print(f"Zoo coverage merged into output: {mapped_zoo_sites:,} mapped PTCI sites")

    out_df = merged.copy()
    out_df["Date"] = pd.to_datetime(out_df["Date"])
    out_df.to_excel(out_xlsx, index=False)
    print(f"Wrote {out_xlsx}")

    if not args.no_csv:
        out_csv = root / args.output_csv if not Path(args.output_csv).is_absolute() else Path(args.output_csv)
        out_df.to_csv(out_csv, index=False)
        print(f"Wrote {out_csv}")


if __name__ == "__main__":
    main()
