#!/usr/bin/env python3
"""Bill.com Vendor Payment Processing Script (Lite Demo Version)

This is a self-contained example agent that demonstrates the agent structure.
"""

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def format_date(date_str: str) -> str:
    """Remove leading zeros from month and day."""
    try:
        if pd.isna(date_str):
            return date_str
        parts = str(date_str).split("/")
        if len(parts) == 3:
            return f"{int(parts[0])}/{int(parts[1])}/{parts[2]}"
        return date_str
    except ValueError:
        return date_str


def format_amount(value: str) -> int | float | str:
    """Remove .00 suffix from whole numbers."""
    try:
        if pd.isna(value):
            return value
        num = float(value)
        return int(num) if num == int(num) else num
    except ValueError:
        return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Process Bill.com vendor payments")
    parser.add_argument(
        "excel_file", help="Path to Excel file with all required sheets"
    )
    parser.add_argument("--date", help="Processing date (YYYY-MM-DD)", default=None)
    args = parser.parse_args()

    # Parse date
    if args.date:
        try:
            processing_date = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            logger.error(f"Invalid date format: {args.date}")
            return 1
    else:
        processing_date = datetime.now()

    excel_file = Path(args.excel_file)
    current_date = processing_date.strftime("%m.%d.%y")
    output_dir = Path(os.getenv("OUTPUT_DIR", Path.cwd() / "outputs"))
    output_dir.mkdir(parents=True, exist_ok=True)

    if not excel_file.exists():
        logger.error(f"File not found: {excel_file}")
        return 1

    logger.info(f"\n{'=' * 50}")
    logger.info("Bill.com Vendor Payment Processing")
    logger.info(f"{'=' * 50}")
    logger.info(f"Source: {excel_file.name} | Date: {current_date}")

    # Load Excel file
    xl_file = pd.ExcelFile(excel_file)

    # Find source sheet
    source_sheet = next(
        (s for s in xl_file.sheet_names if s.lower().startswith("report")), None
    )
    if not source_sheet:
        logger.error("No sheet starting with 'report' found")
        return 1

    logger.info(f"\n[1/7] Loading source data from '{source_sheet}'...")
    df_working = pd.read_excel(excel_file, sheet_name=source_sheet)
    original_count = len(df_working)
    logger.info(f"      Loaded {original_count} records")

    # Validate required sheets
    required_tabs = [
        "Payment Hold List",
        "Vendor Name Changes",
        "Unpaid Uploads",
        "Previously Uploaded Sheet",
    ]
    missing = [t for t in required_tabs if t not in xl_file.sheet_names]
    if missing:
        logger.error(f"Missing sheets: {', '.join(missing)}")
        return 1

    # Save backup
    csv_filename = output_dir / (source_sheet + ".csv")
    backup_filename = output_dir / (source_sheet + "_raw.csv")
    df_working.to_csv(backup_filename, index=False, encoding="utf-8")

    # Step 2: Remove payment holds
    logger.info("\n[2/7] Filtering payment hold vendors...")
    df_payment_holds = pd.read_excel(excel_file, sheet_name="Payment Hold List")
    hold_vendors = [
        str(v).lower() for v in df_payment_holds["Payment Hold List"].dropna()
    ]
    mask = df_working["Vendor Name"].str.lower().isin(hold_vendors)
    df_removed_holds = df_working[mask].copy()
    df_working = df_working[~mask].copy()
    logger.info(f"      Removed {len(df_removed_holds)} payments on hold")

    # Step 3: Normalize dates
    logger.info("\n[3/7] Normalizing date fields...")
    for col in ["Invoice Date", "Due Date", "Approval Date"]:
        df_working[col] = pd.to_datetime(
            df_working[col], format="mixed", errors="coerce"
        )

    current_month, current_year = processing_date.month, processing_date.year
    date_updates = 0

    for idx in df_working.index:
        for col in ["Invoice Date", "Due Date", "Approval Date"]:
            d = df_working.loc[idx, col]
            if pd.notna(d) and (
                d.year < current_year
                or (d.year == current_year and d.month < current_month)
            ):
                df_working.loc[idx, col] = processing_date
                date_updates += 1

    for col in ["Invoice Date", "Due Date", "Approval Date"]:
        df_working[col] = df_working[col].apply(
            lambda x: f"{x.month}/{x.day}/{x.strftime('%y')}" if pd.notna(x) else x
        )
    logger.info(f"      Updated {date_updates} date values")

    # Step 4: Update vendor names
    logger.info("\n[4/7] Standardizing vendor names...")
    df_names = pd.read_excel(excel_file, sheet_name="Vendor Name Changes")
    name_map = dict(
        zip(df_names["Original Vendor Name"], df_names["Corrected Vendor Name"])
    )
    name_updates = 0
    for idx, row in df_working.iterrows():
        if row["Vendor Name"] in name_map:
            df_working.at[idx, "Vendor Name"] = name_map[row["Vendor Name"]]
            name_updates += 1
    logger.info(f"      Updated {name_updates} vendor names")

    # Step 5: Remove previously uploaded
    logger.info("\n[5/7] Removing previously uploaded invoices...")
    df_unpaid = pd.read_excel(excel_file, sheet_name="Unpaid Uploads")
    df_prev = pd.read_excel(excel_file, sheet_name="Previously Uploaded Sheet")
    unpaid = set(df_unpaid["Invoice Number"].astype(str).str.strip())
    prev = set(df_prev["Invoice Number"].astype(str).str.strip())
    paid = prev - unpaid

    df_working["Invoice Number"] = df_working["Invoice Number"].astype(str).str.strip()
    mask = df_working["Invoice Number"].isin(paid)
    df_removed_paid = df_working[mask].copy()
    df_working = df_working[~mask].copy()
    logger.info(f"      Removed {len(df_removed_paid)} paid invoices")

    # Step 6: Add Bill.com fields
    logger.info("\n[6/7] Adding Bill.com required fields...")
    df_working["Bill Payment ID"] = range(1, len(df_working) + 1)
    df_working["GL Account"] = "60000"

    # Format output
    df_working["Amount"] = df_working["Amount"].apply(format_amount)
    for col in ["Invoice Date", "Due Date", "Approval Date"]:
        df_working[col] = df_working[col].apply(format_date)

    # Save main file
    main_filename = output_dir / f"Bill.com Vendor Payments {current_date}.csv"
    df_working.to_csv(
        main_filename, index=False, float_format="%.15g", encoding="utf-8"
    )

    # Step 7: Create audit file
    logger.info("\n[7/7] Generating audit trail...")
    audit_filename = output_dir / f"Vendor Payment Processing Audit {current_date}.xlsx"
    with pd.ExcelWriter(audit_filename, engine="openpyxl") as writer:
        df_working.to_excel(writer, sheet_name="Vendor Payments", index=False)
        pd.read_csv(backup_filename, encoding="utf-8").to_excel(
            writer, sheet_name="Raw Data", index=False
        )
        df_removed_holds.to_excel(writer, sheet_name="Payment Holds", index=False)
        df_removed_paid.to_excel(writer, sheet_name="Previously Uploaded", index=False)

    # Cleanup temporary files
    for f in [csv_filename, backup_filename]:
        if f.exists():
            f.unlink()

    # Summary
    final_count = len(df_working)
    final_amount = df_working["Amount"].astype(float).sum()

    logger.info(f"\n{'=' * 50}")
    logger.info("PROCESSING COMPLETE")
    logger.info(f"{'=' * 50}")
    logger.info(f"Input:  {original_count} records")
    logger.info(f"Output: {final_count} payments | ${final_amount:,.2f}")
    logger.info("\nFiles:")
    logger.info(f"  - {main_filename.name}")
    logger.info(f"  - {audit_filename.name}")
    logger.info(f"{'=' * 50}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
