#!/usr/bin/env python3
"""Bill.com Vendor Payment Processing Script"""

import sys
import logging
from pathlib import Path
from datetime import datetime

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


PRIMARY_SHEET = "Sheet 1"
HOLD_SHEET = "Sheet 2"
NAME_MAP_SHEET = "Vendor Name Changes"
PREV_SHEET = "Previously Uploaded Sheet"
UNPAID_SHEET = "Unpaid Uploads"

HOLD_COL = "Payment Hold List"
NAME_MAP_ORIG_COL = "Vendor"
NAME_MAP_CORR_COL = "Corrected Vendor Name"

OUTPUT_COLUMNS = [
    "Vendor Name",
    "Invoice Number",
    "Invoice Date",
    "Due Date",
    "Amount",
    "Category",
    "Description",
    "Status",
    "Approval Date",
    "Payment Method",
    "Bill Payment ID",
    "GL Account",
]

RAW_COLUMNS = [
    "Vendor Name",
    "Invoice Number",
    "Invoice Date",
    "Due Date",
    "Amount",
    "Category",
    "Description",
    "Status",
    "Approval Date",
    "Payment Method",
]


def parse_date(value, processing_date):
    if pd.isna(value) or value == "":
        return pd.NaT
    dt = pd.to_datetime(value, errors="coerce")
    if pd.isna(dt):
        return pd.NaT
    if dt.year < processing_date.year or (dt.year == processing_date.year and dt.month < processing_date.month):
        dt = processing_date
    return dt


def format_date(value):
    if pd.isna(value):
        return value
    return f"{value.month}/{value.day}/{value.strftime('%y')}"


def format_amount(value):
    if pd.isna(value) or value == "":
        return value
    try:
        num = float(value)
        return int(num) if num == int(num) else num
    except Exception:
        return value


def clean_str_series(s):
    return s.astype(str).str.strip()


def main():
    if len(sys.argv) < 3:
        logger.error("Usage: script.py input_file output_dir")
        return 1

    input_file = Path(sys.argv[1])
    output_dir = Path(sys.argv[2])
    output_dir.mkdir(parents=True, exist_ok=True)

    processing_date = datetime.now()
    current_tag = processing_date.strftime("%m.%d.%y")

    if not input_file.exists():
        logger.error(f"Input file not found: {input_file}")
        return 1

    xl = pd.ExcelFile(input_file)

    if PRIMARY_SHEET not in xl.sheet_names:
        logger.error(f"Missing primary sheet: {PRIMARY_SHEET}")
        return 1
    for req in [HOLD_SHEET, NAME_MAP_SHEET, PREV_SHEET, UNPAID_SHEET]:
        if req not in xl.sheet_names:
            logger.error(f"Missing required sheet: {req}")
            return 1

    df_raw = pd.read_excel(input_file, sheet_name=PRIMARY_SHEET)
    df_raw = df_raw.copy()
    original_count = len(df_raw)

    # Preserve raw data for audit
    df_raw_audit = df_raw[RAW_COLUMNS].copy()

    # Load reference sheets
    df_holds = pd.read_excel(input_file, sheet_name=HOLD_SHEET)
    df_name_map = pd.read_excel(input_file, sheet_name=NAME_MAP_SHEET)
    df_prev = pd.read_excel(input_file, sheet_name=PREV_SHEET)
    df_unpaid = pd.read_excel(input_file, sheet_name=UNPAID_SHEET)

    # Normalize key fields for matching
    df_raw["Vendor Name"] = df_raw["Vendor Name"].astype(str).str.strip()
    df_raw["Invoice Number"] = df_raw["Invoice Number"].astype(str).str.strip()

    hold_vendors = set(clean_str_series(df_holds[HOLD_COL]).str.lower()) if HOLD_COL in df_holds.columns else set()

    # Payment holds filter
    hold_mask = df_raw["Vendor Name"].str.lower().isin(hold_vendors)
    df_payment_holds = df_raw.loc[hold_mask, RAW_COLUMNS].copy()
    df_work = df_raw.loc[~hold_mask].copy()

    # Previously uploaded vs unpaid logic
    prev_invoices = set(clean_str_series(df_prev["Invoice Number"])) if "Invoice Number" in df_prev.columns else set()
    unpaid_invoices = set(clean_str_series(df_unpaid["Invoice Number"])) if "Invoice Number" in df_unpaid.columns else set()
    paid_invoices = prev_invoices - unpaid_invoices

    paid_mask = df_work["Invoice Number"].isin(paid_invoices)
    df_previously_uploaded = df_work.loc[paid_mask, RAW_COLUMNS].copy()
    df_work = df_work.loc[~paid_mask].copy()

    # Standardize vendor names
    name_map = {}
    if NAME_MAP_ORIG_COL in df_name_map.columns and NAME_MAP_CORR_COL in df_name_map.columns:
        for _, r in df_name_map.iterrows():
            orig = str(r[NAME_MAP_ORIG_COL]).strip()
            corr = str(r[NAME_MAP_CORR_COL]).strip()
            if orig and orig.lower() != "nan" and corr and corr.lower() != "nan":
                name_map[orig] = corr

    df_work["Vendor Name"] = df_work["Vendor Name"].apply(lambda x: name_map.get(str(x).strip(), str(x).strip()))

    # Normalize dates and format output
    for col in ["Invoice Date", "Due Date", "Approval Date"]:
        df_work[col] = df_work[col].apply(lambda x: parse_date(x, processing_date))
        df_work[col] = df_work[col].apply(format_date)

    # Amount formatting
    df_work["Amount"] = df_work["Amount"].apply(format_amount)

    # Build final Vendor Payments output
    df_vendor_payments = df_work[RAW_COLUMNS].copy()
    df_vendor_payments["Bill Payment ID"] = range(1, len(df_vendor_payments) + 1)
    df_vendor_payments["GL Account"] = "60000"
    df_vendor_payments = df_vendor_payments[OUTPUT_COLUMNS]

    # Audit workbook sheets
    audit_path = output_dir / f"Vendor Payment Processing Audit {current_tag}.xlsx"
    csv_path = output_dir / f"Bill.com Vendor Payments {current_tag}.csv"

    with pd.ExcelWriter(audit_path, engine="openpyxl") as writer:
        df_vendor_payments.to_excel(writer, sheet_name="Vendor Payments", index=False)
        df_raw_audit.to_excel(writer, sheet_name="Raw Data", index=False)
        df_payment_holds.to_excel(writer, sheet_name="Payment Holds", index=False)
        df_previously_uploaded.to_excel(writer, sheet_name="Previously Uploaded", index=False)

    df_vendor_payments.to_csv(csv_path, index=False, encoding="utf-8")

    logger.info(f"Processed {original_count} rows -> {len(df_vendor_payments)} vendor payments")
    logger.info(f"Audit file: {audit_path}")
    logger.info(f"CSV file: {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
