#!/usr/bin/env python3
"""Bill.com Vendor Payment Processing Script

Transforms vendor payment records from Excel into Bill.com-ready format with
comprehensive audit trail and filtering.
"""

import sys
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def format_date(date_val) -> str:
    """Convert date to M/D/YY format without leading zeros."""
    try:
        if pd.isna(date_val):
            return ""
        if isinstance(date_val, str):
            date_val = pd.to_datetime(date_val, format="mixed", errors="coerce")
        if pd.isna(date_val):
            return ""
        return f"{int(date_val.month)}/{int(date_val.day)}/{date_val.strftime('%y')}"
    except (ValueError, AttributeError):
        return ""


def format_amount(value) -> int | float:
    """Remove .00 suffix from whole numbers."""
    try:
        if pd.isna(value):
            return value
        num = float(value)
        return int(num) if num == int(num) else num
    except (ValueError, TypeError):
        return value


def normalize_date_to_current(date_val, processing_date):
    """Replace past dates with processing date."""
    try:
        if pd.isna(date_val):
            return date_val
        if isinstance(date_val, str):
            date_val = pd.to_datetime(date_val, format="mixed", errors="coerce")
        if pd.isna(date_val):
            return date_val
        
        if (date_val.year < processing_date.year or 
            (date_val.year == processing_date.year and date_val.month < processing_date.month)):
            return processing_date
        return date_val
    except (ValueError, AttributeError):
        return date_val


def main() -> int:
    """Main processing pipeline."""
    
    # Parse arguments
    if len(sys.argv) < 3:
        logger.error("Usage: python script.py <input_file> <output_dir>")
        return 1
    
    input_file = Path(sys.argv[1])
    output_dir = Path(sys.argv[2])
    
    if not input_file.exists():
        logger.error(f"Input file not found: {input_file}")
        return 1
    
    output_dir.mkdir(parents=True, exist_ok=True)
    processing_date = datetime.now()
    current_date_str = processing_date.strftime("%m.%d.%y")
    
    logger.info(f"\n{'=' * 60}")
    logger.info("Bill.com Vendor Payment Processing")
    logger.info(f"{'=' * 60}")
    logger.info(f"Source: {input_file.name} | Processing Date: {current_date_str}")
    
    # Load Excel file
    try:
        xl_file = pd.ExcelFile(input_file)
    except Exception as e:
        logger.error(f"Failed to read Excel file: {e}")
        return 1
    
    # Validate required sheets
    required_sheets = {
        "Sheet 1": "Primary Vendor Payment Records",
        "Sheet 2": "Payment Hold List",
        "Abhijeet": "Vendor Name Standardization Mapping",
        "Previously Uploaded Sheet": "Previously Processed Invoice Registry",
        "Unpaid Uploads": "Unpaid Invoice Tracking",
    }
    
    missing = [sheet for sheet in required_sheets.keys() if sheet not in xl_file.sheet_names]
    if missing:
        logger.error(f"Missing required sheets: {', '.join(missing)}")
        return 1
    
    # Step 1: Load raw data
    logger.info("\n[1/8] Loading source data from 'Sheet 1'...")
    try:
        df_raw = pd.read_excel(input_file, sheet_name="Sheet 1")
    except Exception as e:
        logger.error(f"Failed to read Sheet 1: {e}")
        return 1
    
    original_count = len(df_raw)
    logger.info(f"      Loaded {original_count} records")
    
    # Create working copy
    df_working = df_raw.copy()
    
    # Step 2: Filter payment holds
    logger.info("\n[2/8] Filtering payment hold vendors...")
    try:
        df_holds_list = pd.read_excel(input_file, sheet_name="Sheet 2")
        hold_vendors = set(
            str(v).strip().lower() 
            for v in df_holds_list.iloc[:, 0].dropna()
        )
    except Exception as e:
        logger.error(f"Failed to read Payment Hold List: {e}")
        return 1
    
    # Case-insensitive match on "Name" column
    mask_holds = df_working["Name"].str.lower().isin(hold_vendors)
    df_payment_holds = df_working[mask_holds].copy()
    df_working = df_working[~mask_holds].copy()
    logger.info(f"      Removed {len(df_payment_holds)} payments on hold")
    
    # Step 3: Load reference data
    logger.info("\n[3/8] Loading reference data...")
    try:
        df_vendor_map = pd.read_excel(input_file, sheet_name="Abhijeet")
        df_prev_uploaded = pd.read_excel(input_file, sheet_name="Previously Uploaded Sheet")
        df_unpaid = pd.read_excel(input_file, sheet_name="Unpaid Uploads")
    except Exception as e:
        logger.error(f"Failed to read reference sheets: {e}")
        return 1
    
    # Step 4: Normalize dates
    logger.info("\n[4/8] Normalizing date fields...")
    date_columns = ["Invoice Date", "Due Date", "Date"]
    
    for col in date_columns:
        if col in df_working.columns:
            df_working[col] = pd.to_datetime(df_working[col], format="mixed", errors="coerce")
            df_working[col] = df_working[col].apply(
                lambda x: normalize_date_to_current(x, processing_date)
            )
    
    logger.info(f"      Normalized {len(df_working)} date values")
    
    # Step 5: Standardize vendor names
    logger.info("\n[5/8] Standardizing vendor names...")
    vendor_map = dict(zip(
        df_vendor_map["Vendor"].astype(str).str.strip(),
        df_vendor_map["Corrected Vendor Name"].astype(str).str.strip()
    ))
    
    name_updates = 0
    for idx in df_working.index:
        vendor = str(df_working.loc[idx, "Name"]).strip()
        if vendor in vendor_map:
            df_working.at[idx, "Name"] = vendor_map[vendor]
            name_updates += 1
    
    logger.info(f"      Updated {name_updates} vendor names")
    
    # Step 6: Remove previously paid invoices
    logger.info("\n[6/8] Removing previously paid invoices...")
    unpaid_set = set(
        str(inv).strip() 
        for inv in df_unpaid["Invoice Number"].dropna()
    )
    prev_set = set(
        str(inv).strip() 
        for inv in df_prev_uploaded["Invoice Number"].dropna()
    )
    paid_set = prev_set - unpaid_set
    
    df_working["Invoice Number"] = df_working["Invoice Number"].astype(str).str.strip()
    mask_paid = df_working["Invoice Number"].isin(paid_set)
    df_previously_uploaded = df_working[mask_paid].copy()
    df_working = df_working[~mask_paid].copy()
    logger.info(f"      Removed {len(df_previously_uploaded)} paid invoices")
    
    # Step 7: Add Bill.com fields
    logger.info("\n[7/8] Adding Bill.com required fields...")
    df_working["Bill Payment ID"] = range(1, len(df_working) + 1)
    df_working["GL Account"] = "60000"
    
    # Step 8: Format output columns
    logger.info("\n[8/8] Formatting output...")
    
    # Format amounts
    df_working["Amount"] = df_working["Amount"].apply(format_amount)
    
    # Format dates
    for col in date_columns:
        if col in df_working.columns:
            df_working[col] = df_working[col].apply(format_date)
    
    # Rename columns for output
    output_columns = [
        "Name",
        "Invoice Number",
        "Invoice Date",
        "Due Date",
        "Amount",
        "Category",
        "Description",
        "Status",
        "Date",
        "Method",
        "Bill Payment ID",
        "GL Account",
    ]
    
    # Ensure all columns exist
    for col in output_columns:
        if col not in df_working.columns:
            df_working[col] = ""
    
    df_vendor_payments = df_working[output_columns].copy()
    
    # Rename columns to semantic names for output
    df_vendor_payments = df_vendor_payments.rename(columns={
        "Name": "Vendor Name",
        "Date": "Approval Date",
        "Method": "Payment Method",
    })
    
    # Prepare raw data sheet (no transformations)
    df_raw_output = df_raw.copy()
    
    # Prepare payment holds sheet
    df_payment_holds_output = df_payment_holds.copy()
    if len(df_payment_holds_output) > 0:
        df_payment_holds_output = df_payment_holds_output.rename(columns={
            "Name": "Vendor Name",
            "Date": "Approval Date",
            "Method": "Payment Method",
        })
    
    # Prepare previously uploaded sheet
    df_previously_uploaded_output = df_previously_uploaded.copy()
    if len(df_previously_uploaded_output) > 0:
        df_previously_uploaded_output = df_previously_uploaded_output.rename(columns={
            "Name": "Vendor Name",
            "Date": "Approval Date",
            "Method": "Payment Method",
        })
    
    # Write Excel audit file
    audit_filename = output_dir / f"Vendor Payment Processing Audit {current_date_str}.xlsx"
    try:
        with pd.ExcelWriter(audit_filename, engine="openpyxl") as writer:
            df_vendor_payments.to_excel(writer, sheet_name="Vendor Payments", index=False)
            df_raw_output.to_excel(writer, sheet_name="Raw Data", index=False)
            df_payment_holds_output.to_excel(writer, sheet_name="Payment Holds", index=False)
            df_previously_uploaded_output.to_excel(writer, sheet_name="Previously Uploaded", index=False)
        logger.info(f"      Created audit file: {audit_filename.name}")
    except Exception as e:
        logger.error(f"Failed to write audit file: {e}")
        return 1
    
    # Write CSV for Bill.com upload
    csv_filename = output_dir / f"Bill.com Vendor Payments {current_date_str}.csv"
    try:
        df_vendor_payments.to_csv(csv_filename, index=False, encoding="utf-8")
        logger.info(f"      Created CSV file: {csv_filename.name}")
    except Exception as e:
        logger.error(f"Failed to write CSV file: {e}")
        return 1
    
    # Summary
    final_count = len(df_vendor_payments)
    final_amount = pd.to_numeric(df_vendor_payments["Amount"], errors="coerce").sum()
    
    logger.info(f"\n{'=' * 60}")
    logger.info("PROCESSING COMPLETE")
    logger.info(f"{'=' * 60}")
    logger.info(f"Input:  {original_count} records")
    logger.info(f"Output: {final_count} payments | ${final_amount:,.2f}")
    logger.info(f"\nBreakdown:")
    logger.info(f"  - Payment Holds: {len(df_payment_holds)}")
    logger.info(f"  - Previously Paid: {len(df_previously_uploaded)}")
    logger.info(f"\nOutput Files:")
    logger.info(f"  - {audit_filename.name}")
    logger.info(f"  - {csv_filename.name}")
    logger.info(f"{'=' * 60}\n")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())