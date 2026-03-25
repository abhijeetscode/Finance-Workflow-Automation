#!/usr/bin/env python3
"""Bill.com Vendor Payment Processing Script

Transforms vendor invoice data into Bill.com payment format with comprehensive
audit trail and filtering logic.
"""

import argparse
import logging
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
	"""Remove leading zeros from month and day in date strings."""
	try:
		if pd.isna(date_str):
			return date_str
		parts = str(date_str).split("/")
		if len(parts) == 3:
			return f"{int(parts[0])}/{int(parts[1])}/{parts[2]}"
		return date_str
	except (ValueError, AttributeError):
		return date_str


def format_amount(value) -> int | float | str:
	"""Remove .00 suffix from whole numbers."""
	try:
		if pd.isna(value):
			return value
		num = float(value)
		return int(num) if num == int(num) else num
	except (ValueError, TypeError):
		return value


def normalize_date_field(date_val, processing_date):
	"""Parse date and forward-date if in the past."""
	try:
		if pd.isna(date_val):
			return date_val

		# Parse the date
		parsed_date = pd.to_datetime(date_val, format="mixed", errors="coerce")
		if pd.isna(parsed_date):
			return date_val

		# Forward-date if in the past
		if parsed_date.year < processing_date.year or (
			parsed_date.year == processing_date.year and parsed_date.month < processing_date.month
		):
			parsed_date = processing_date

		# Format as M/D/YY
		return f"{parsed_date.month}/{parsed_date.day}/{parsed_date.strftime('%y')}"
	except Exception:
		return date_val


def main() -> int:
	parser = argparse.ArgumentParser(description="Process Bill.com vendor payments from Excel file")
	parser.add_argument("input_file", help="Path to input Excel file")
	parser.add_argument("output_dir", help="Path to output directory")
	parser.add_argument("--date", help="Processing date (YYYY-MM-DD format)", default=None)

	args = parser.parse_args()

	# Parse processing date
	if args.date:
		try:
			processing_date = datetime.strptime(args.date, "%Y-%m-%d")
		except ValueError:
			logger.error(f"Invalid date format: {args.date}. Use YYYY-MM-DD")
			return 1
	else:
		processing_date = datetime.now()

	input_file = Path(args.input_file)
	output_dir = Path(args.output_dir)
	output_dir.mkdir(parents=True, exist_ok=True)

	if not input_file.exists():
		logger.error(f"Input file not found: {input_file}")
		return 1

	current_date_str = processing_date.strftime("%m.%d.%y")

	logger.info(f"\n{'=' * 60}")
	logger.info("Bill.com Vendor Payment Processing")
	logger.info(f"{'=' * 60}")
	logger.info(f"Source: {input_file.name}")
	logger.info(f"Processing Date: {processing_date.strftime('%Y-%m-%d')}")
	logger.info(f"Output Directory: {output_dir}")

	# Load Excel file
	try:
		xl_file = pd.ExcelFile(input_file)
	except Exception as e:
		logger.error(f"Failed to read Excel file: {e}")
		return 1

	logger.info(f"Available sheets: {xl_file.sheet_names}")

	# Validate required sheets
	required_sheets = {
		"primary": "Sheet 1",
		"holds": "Sheet 2",
		"vendor_map": "Abhijeet",
		"prev_uploaded": "Previously Uploaded Sheet",
		"unpaid": "Unpaid Uploads",
	}

	for key, sheet_name in required_sheets.items():
		if sheet_name not in xl_file.sheet_names:
			logger.error(f"Required sheet '{sheet_name}' not found")
			return 1

	# Step 1: Load primary invoice data
	logger.info("\n[1/7] Loading primary invoice data...")
	try:
		df_working = pd.read_excel(input_file, sheet_name="Sheet 1")
		original_count = len(df_working)
		logger.info(f"      Loaded {original_count} records")

		# Rename columns to semantic names for processing
		column_mapping = {
			"Name": "Vendor Name",
			"Invoice Number": "Invoice Number",
			"Invoice Date": "Invoice Date",
			"Due Date": "Due Date",
			"Amount": "Amount",
			"Category": "Category",
			"Description": "Description",
			"Status": "Status",
			"Date": "Approval Date",
			"Method": "Payment Method",
		}

		df_working = df_working.rename(columns=column_mapping)

		# Save raw data for audit
		df_raw = df_working.copy()

	except Exception as e:
		logger.error(f"Failed to load primary data: {e}")
		return 1

	# Step 2: Filter payment hold vendors
	logger.info("\n[2/7] Filtering payment hold vendors...")
	try:
		df_holds_list = pd.read_excel(input_file, sheet_name="Sheet 2")
		hold_vendors = set(
			str(v).strip().lower() for v in df_holds_list["Payment Hold List"].dropna()
		)

		# Identify records with payment holds
		mask_holds = df_working["Vendor Name"].str.lower().isin(hold_vendors)
		df_payment_holds = df_working[mask_holds].copy()
		df_working = df_working[~mask_holds].copy()

		logger.info(f"      Removed {len(df_payment_holds)} payments on hold")
	except Exception as e:
		logger.error(f"Failed to process payment holds: {e}")
		return 1

	# Step 3: Normalize date fields
	logger.info("\n[3/7] Normalizing date fields...")
	try:
		date_columns = ["Invoice Date", "Due Date", "Approval Date"]
		date_updates = 0

		for col in date_columns:
			if col in df_working.columns:
				df_working[col] = df_working[col].apply(
					lambda x: normalize_date_field(x, processing_date)
				)
				date_updates += len(df_working)

		logger.info(f"      Normalized {len(date_columns)} date columns")
	except Exception as e:
		logger.error(f"Failed to normalize dates: {e}")
		return 1

	# Step 4: Standardize vendor names
	logger.info("\n[4/7] Standardizing vendor names...")
	try:
		df_vendor_map = pd.read_excel(input_file, sheet_name="Abhijeet")

		# Create mapping from Original to Corrected
		vendor_name_map = dict(
			zip(
				df_vendor_map["Vendor"].astype(str).str.strip(),
				df_vendor_map["Corrected Vendor Name"].astype(str).str.strip(),
			)
		)

		name_updates = 0
		for idx in df_working.index:
			vendor = df_working.at[idx, "Vendor Name"]
			if vendor in vendor_name_map:
				df_working.at[idx, "Vendor Name"] = vendor_name_map[vendor]
				name_updates += 1

		logger.info(f"      Updated {name_updates} vendor names")
	except Exception as e:
		logger.error(f"Failed to standardize vendor names: {e}")
		return 1

	# Step 5: Remove previously paid invoices
	logger.info("\n[5/7] Removing previously paid invoices...")
	try:
		df_unpaid = pd.read_excel(input_file, sheet_name="Unpaid Uploads")
		df_prev_uploaded = pd.read_excel(input_file, sheet_name="Previously Uploaded Sheet")

		# Get sets of invoice numbers
		unpaid_invoices = set(df_unpaid["Invoice Number"].astype(str).str.strip())
		prev_uploaded_invoices = set(df_prev_uploaded["Invoice Number"].astype(str).str.strip())

		# Paid invoices = Previously Uploaded - Unpaid
		paid_invoices = prev_uploaded_invoices - unpaid_invoices

		# Normalize invoice numbers in working data
		df_working["Invoice Number"] = df_working["Invoice Number"].astype(str).str.strip()

		# Identify paid invoices to remove
		mask_paid = df_working["Invoice Number"].isin(paid_invoices)
		df_previously_uploaded = df_working[mask_paid].copy()
		df_working = df_working[~mask_paid].copy()

		logger.info(f"      Removed {len(df_previously_uploaded)} paid invoices")
	except Exception as e:
		logger.error(f"Failed to process previously uploaded invoices: {e}")
		return 1

	# Step 6: Add Bill.com required fields
	logger.info("\n[6/7] Adding Bill.com required fields...")
	try:
		df_working["Bill Payment ID"] = range(1, len(df_working) + 1)
		df_working["GL Account"] = "60000"

		# Format amounts
		df_working["Amount"] = df_working["Amount"].apply(format_amount)

		# Format dates (remove leading zeros)
		for col in ["Invoice Date", "Due Date", "Approval Date"]:
			if col in df_working.columns:
				df_working[col] = df_working[col].apply(format_date)

		logger.info("      Added Bill Payment IDs and GL Accounts")
	except Exception as e:
		logger.error(f"Failed to add Bill.com fields: {e}")
		return 1

	# Step 7: Generate output files
	logger.info("\n[7/7] Generating output files...")
	try:
		# Prepare output column order
		output_columns = [
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

		df_output = df_working[output_columns].copy()

		# Create audit Excel file
		audit_filename = output_dir / f"Vendor Payment Processing Audit {current_date_str}.xlsx"
		with pd.ExcelWriter(audit_filename, engine="openpyxl") as writer:
			df_output.to_excel(writer, sheet_name="Vendor Payments", index=False)
			df_raw.to_excel(writer, sheet_name="Raw Data", index=False)
			df_payment_holds.to_excel(writer, sheet_name="Payment Holds", index=False)
			df_previously_uploaded.to_excel(writer, sheet_name="Previously Uploaded", index=False)

		logger.info(f"      Created audit file: {audit_filename.name}")

		# Create CSV file for Bill.com upload
		csv_filename = output_dir / f"Bill.com Vendor Payments {current_date_str}.csv"
		df_output.to_csv(csv_filename, index=False, encoding="utf-8")
		logger.info(f"      Created CSV file: {csv_filename.name}")

	except Exception as e:
		logger.error(f"Failed to generate output files: {e}")
		return 1

	# Summary
	final_count = len(df_working)
	try:
		final_amount = pd.to_numeric(df_working["Amount"], errors="coerce").sum()
	except Exception:
		final_amount = 0

	logger.info(f"\n{'=' * 60}")
	logger.info("PROCESSING COMPLETE")
	logger.info(f"{'=' * 60}")
	logger.info(f"Input Records:        {original_count}")
	logger.info(f"Payment Holds:        {len(df_payment_holds)}")
	logger.info(f"Previously Paid:      {len(df_previously_uploaded)}")
	logger.info(f"Output Records:       {final_count}")
	logger.info(f"Total Amount:         ${final_amount:,.2f}")
	logger.info("\nOutput Files:")
	logger.info(f"  - {audit_filename.name}")
	logger.info(f"  - {csv_filename.name}")
	logger.info(f"{'=' * 60}\n")

	return 0


if __name__ == "__main__":
	sys.exit(main())
