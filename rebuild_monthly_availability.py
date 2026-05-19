#!/usr/bin/env python3
from __future__ import annotations

import argparse
import calendar
import logging
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from operations_kpi_logging import add_log_level_arg, configure_logging, log_timing
from merge_daily_availability_overall import (
    LEGACY_COLUMNS,
    DAILY_SITE_AVAILABILITY_SHEET,
    dedupe_key_series,
    filter_rows_to_tab_month,
    harmonize_columns,
    merge_dispatch_metrics,
    merge_zoo_mapping,
    normalize_for_merge,
)
from transform_daily_availability_robust import transform_daily_availability

logger = logging.getLogger("operations_kpi.etl.rebuild_monthly_availability")


def month_from_filename(path: Path) -> tuple[datetime, str]:
    match = re.match(r"([A-Za-z]+)_(\d{4})_", path.name)
    if not match:
        raise ValueError(f"Could not parse month/year from filename: {path.name}")

    month_token, year_text = match.groups()
    year = int(year_text)

    month_lookup = {}
    for idx in range(1, 13):
        month_lookup[calendar.month_name[idx].lower()] = idx
        month_lookup[calendar.month_abbr[idx].lower()] = idx

    month = month_lookup.get(month_token.lower())
    if month is None:
        raise ValueError(f"Unknown month token {month_token!r} in filename {path.name}")

    dt = datetime(year, month, 1)
    label = f"{calendar.month_abbr[month]}_{year}_Daily_Site_Availability"
    return dt, label


def monthly_workbooks(folder: Path) -> list[tuple[Path, datetime, str]]:
    entries = []
    for path in folder.glob("*.xlsx"):
        dt, label = month_from_filename(path)
        entries.append((path, dt, label))
    if not entries:
        raise ValueError(f"No .xlsx files found in {folder}")
    entries.sort(key=lambda item: (item[1].year, item[1].month, item[0].name.lower()))
    return entries


def transpose_monthly_workbooks(entries: list[tuple[Path, datetime, str]]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path, tab_dt, label in entries:
        logger.info(
            "Monthly workbook %r (%s) -> %s",
            label,
            path.name,
            tab_dt.strftime("%Y-%m"),
        )
        raw = transform_daily_availability(
            str(path),
            sheet_name=DAILY_SITE_AVAILABILITY_SHEET,
            header_row=2,
            data_start_row=3,
            data_only=True,
        )
        raw = filter_rows_to_tab_month(raw, label, tab_dt)
        frames.append(harmonize_columns(raw))

    combined = pd.concat(frames, ignore_index=True)
    combined["Date"] = pd.to_datetime(combined["Date"]).dt.normalize()
    return combined


def dedupe_monthly_data(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    normalized = normalize_for_merge(df.reindex(columns=LEGACY_COLUMNS))
    normalized["_dedupe_key"] = dedupe_key_series(normalized)
    before = len(normalized)
    normalized = normalized.sort_values(["_dedupe_key", "Date"], kind="mergesort")
    normalized = normalized.drop_duplicates(subset=["_dedupe_key"], keep="last")
    dropped = before - len(normalized)
    normalized = normalized.drop(columns=["_dedupe_key"])
    normalized = normalized.sort_values(["Date", "PLA ID", "PTCI Number"], kind="mergesort")
    normalized = normalized.reset_index(drop=True)
    return normalized, dropped


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-folder",
        default="monthly_availability_data",
        help="Folder containing monthly availability workbooks.",
    )
    parser.add_argument(
        "--output-csv",
        default="daily_availability_transformed.csv",
        help="Path to write rebuilt CSV.",
    )
    parser.add_argument(
        "--output-xlsx",
        default="daily_availability_transformed.xlsx",
        help="Path to write rebuilt XLSX.",
    )
    parser.add_argument(
        "--dispatch-workbook",
        default="CM Dispatch Daily Distribution V2.xlsx",
        help="Path to CM dispatch workbook used to enrich SIC Count and CM Count.",
    )
    parser.add_argument(
        "--zoo-mapping",
        default="site_site.csv",
        help="Path to Zoo mapping CSV used to enrich the consolidated master data.",
    )
    add_log_level_arg(parser)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_logging(args.log_level)
    root = Path(__file__).resolve().parent
    source_folder = root / args.source_folder if not Path(args.source_folder).is_absolute() else Path(args.source_folder)
    output_csv = root / args.output_csv if not Path(args.output_csv).is_absolute() else Path(args.output_csv)
    output_xlsx = root / args.output_xlsx if not Path(args.output_xlsx).is_absolute() else Path(args.output_xlsx)
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

    try:
        entries = monthly_workbooks(source_folder)
    except ValueError as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc

    logger.info("Monthly files to process:")
    for path, dt, _ in entries:
        logger.info("  - %s -> %s", path.name, dt.strftime("%Y-%m"))

    with log_timing(logger, "transpose_monthly_workbooks", files=len(entries)):
        combined = transpose_monthly_workbooks(entries)
    deduped, dropped = dedupe_monthly_data(combined)
    enriched = merge_dispatch_metrics(deduped, dispatch_path)
    enriched = merge_zoo_mapping(enriched, zoo_mapping_path)

    logger.info("Combined rows before dedupe: %d", len(combined))
    logger.info("Duplicate site/date rows removed: %d", dropped)
    logger.info(
        "Rebuilt shape: %d rows, Date %s .. %s",
        len(enriched),
        enriched["Date"].min().date(),
        enriched["Date"].max().date(),
    )
    logger.info(
        "Dispatch totals merged into output: SIC Count=%s, CM Count=%s",
        f"{int(enriched['SIC Count'].sum()):,}",
        f"{int(enriched['CM Count'].sum()):,}",
    )
    mapped_zoo_sites = int(enriched.loc[enriched["Zoo"].notna(), "PTCI Number"].nunique())
    logger.info(
        "Zoo coverage merged into output: %s mapped PTCI sites",
        f"{mapped_zoo_sites:,}",
    )

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    enriched.to_csv(output_csv, index=False)
    enriched.to_excel(output_xlsx, index=False)
    logger.info("Wrote %s", output_csv)
    logger.info("Wrote %s", output_xlsx)


if __name__ == "__main__":
    main()
