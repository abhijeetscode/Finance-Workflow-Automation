"""
Tool implementations for the SOP agent.

Rules:
- All Excel reads/writes use openpyxl (xlsx) or SpreadsheetML parser (xls/xml)
- All writes are bulk — full dataset in one pass
- Write tools always call verify_before_write before touching any file
- Write tools enforce that the target path is inside fyra/output/

File format notes:
- .xls files from NetSuite are SpreadsheetML (Office XML), not BIFF — use lxml
- .xml report files from NetSuite are HTML-like XML — use BeautifulSoup html.parser
- .xlsx output file uses openpyxl
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

import openpyxl
from bs4 import BeautifulSoup
from lxml import etree  # used for SpreadsheetML parsing only

from human_loop import verify_before_write

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent / "output"
_SS_NS = "urn:schemas-microsoft-com:office:spreadsheet"


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


def _assert_output_path(path: str) -> Path:
	p = Path(path).resolve()
	if not p.is_relative_to(OUTPUT_DIR.resolve()):
		raise ValueError(
			f"Write blocked: {p} is outside the output/ directory. "
			"All writes must target the working copy."
		)
	return p


# ---------------------------------------------------------------------------
# SpreadsheetML helpers (NetSuite .xls and .xml exports)
# ---------------------------------------------------------------------------


def _is_spreadsheetml(path: str) -> bool:
	"""Peek at first 512 bytes to detect SpreadsheetML format."""
	with open(path, "rb") as f:
		header = f.read(512).decode("utf-8", errors="ignore")
	return "schemas-microsoft-com:office:spreadsheet" in header


def _parse_spreadsheetml(path: str) -> etree._ElementTree:
	parser = etree.XMLParser(recover=True)
	return etree.parse(path, parser)


def _spreadsheetml_sheets(path: str) -> list[str]:
	tree = _parse_spreadsheetml(path)
	root = tree.getroot()
	worksheets = root.findall(f".//{{{_SS_NS}}}Worksheet")
	return [ws.get(f"{{{_SS_NS}}}Name", "") for ws in worksheets]


def _read_spreadsheetml_sheet(path: str, sheet_name: str) -> list[list[str]]:
	"""Read a SpreadsheetML worksheet into a list of rows.
	Handles ss:Index for sparse cells (cells that skip column positions)."""
	tree = _parse_spreadsheetml(path)
	root = tree.getroot()

	worksheets = root.findall(f".//{{{_SS_NS}}}Worksheet")
	target = next((ws for ws in worksheets if ws.get(f"{{{_SS_NS}}}Name") == sheet_name), None)
	if target is None:
		available = [ws.get(f"{{{_SS_NS}}}Name") for ws in worksheets]
		raise ValueError(f"Sheet '{sheet_name}' not found. Available: {available}")

	table = target.find(f"{{{_SS_NS}}}Table")
	if table is None:
		return []

	rows_out = []
	for row_elem in table.findall(f"{{{_SS_NS}}}Row"):
		sparse: dict[int, str] = {}
		col_idx = 1
		for cell in row_elem.findall(f"{{{_SS_NS}}}Cell"):
			explicit = cell.get(f"{{{_SS_NS}}}Index")
			if explicit:
				col_idx = int(explicit)
			data = cell.find(f"{{{_SS_NS}}}Data")
			sparse[col_idx] = data.text if data is not None and data.text else ""
			col_idx += 1

		if sparse:
			max_col = max(sparse.keys())
			rows_out.append([sparse.get(i, "") for i in range(1, max_col + 1)])

	return rows_out


# ---------------------------------------------------------------------------
# Excel — read (unified: SpreadsheetML or openpyxl)
# ---------------------------------------------------------------------------


def read_excel(path: str, sheet: str, max_rows: int = 25) -> str:
	"""Read data from an Excel sheet (.xls SpreadsheetML or .xlsx).
	Returns a JSON object with total_rows, columns, and up to max_rows sample rows.
	Rows where all values are empty are skipped.
	Pass max_rows=0 to return all rows (use with caution on large files)."""
	logger.debug("read_excel: %s | sheet=%s", path, sheet)

	p = Path(path)
	if p.suffix.lower() in (".xls", ".xml") or _is_spreadsheetml(path):
		raw_rows = _read_spreadsheetml_sheet(path, sheet)
	else:
		wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
		ws = wb[sheet]
		raw_rows = [
			[str(cell.value) if cell.value is not None else "" for cell in row]
			for row in ws.iter_rows()
		]
		wb.close()

	if not raw_rows:
		return json.dumps({"total_rows": 0, "columns": [], "rows": []})

	headers = [str(h) if h else f"col_{i}" for i, h in enumerate(raw_rows[0])]
	records = []
	for row in raw_rows[1:]:
		padded = (row + [""] * len(headers))[: len(headers)]
		if all(v == "" or v is None for v in padded):
			continue
		records.append(dict(zip(headers, padded)))

	total = len(records)
	sample = records if max_rows == 0 else records[:max_rows]
	logger.debug("read_excel: %d records total, returning %d", total, len(sample))
	return json.dumps(
		{
			"total_rows": total,
			"columns": headers,
			"rows": sample,
			**(
				{
					"note": f"Showing first {len(sample)} of {total} rows. Pass max_rows=0 to get all."
				}
				if max_rows and total > max_rows
				else {}
			),
		}
	)


def list_sheets(path: str) -> str:
	"""List all sheet names in an Excel file (.xls SpreadsheetML or .xlsx).
	Returns JSON array."""
	logger.debug("list_sheets: %s", path)

	p = Path(path)
	if p.suffix.lower() in (".xls", ".xml") or _is_spreadsheetml(path):
		names = _spreadsheetml_sheets(path)
	else:
		wb = openpyxl.load_workbook(path, read_only=True)
		names = wb.sheetnames
		wb.close()

	return json.dumps(names)


# ---------------------------------------------------------------------------
# Excel — write helpers
# ---------------------------------------------------------------------------


def _get_sheet_headers(ws) -> dict[str, int]:
	"""Scan first 10 rows to find the header row.
	Returns {column_name: col_index (1-based)} for the first row with >= 2 non-empty cells."""
	for row in ws.iter_rows(max_row=10):
		non_empty = [(cell.column, str(cell.value)) for cell in row if cell.value is not None]
		if len(non_empty) >= 2:
			return {name: col for col, name in non_empty}
	return {}


def _find_write_row(ws, start_col: int, end_col: int, after_keyword: str = "") -> int:
	"""Find the first empty row in [start_col:end_col] range.
	If after_keyword given, scan only below the row containing that keyword."""
	start_scan = 1

	if after_keyword:
		for row in ws.iter_rows():
			for cell in row:
				if cell.value and after_keyword.lower() in str(cell.value).lower():
					start_scan = cell.row + 1
					break
			if start_scan > 1:
				break

	last_data_row = start_scan - 1
	for row in ws.iter_rows(min_row=start_scan, min_col=start_col, max_col=end_col):
		if any(cell.value is not None for cell in row):
			last_data_row = row[0].row

	return last_data_row + 1


def _preview_rows(rows: list, start_row: int, sheet: str) -> str:
	lines = [f"Sheet: {sheet} | Writing {len(rows)} rows starting at row {start_row}"]
	lines.append(f"First {min(5, len(rows))} rows:")
	for i, r in enumerate(rows[:5]):
		lines.append(f"  Row {start_row + i}: {r}")
	if len(rows) > 5:
		lines.append(f"  ... and {len(rows) - 5} more rows")
	return "\n".join(lines)


# ---------------------------------------------------------------------------
# Excel — write
# ---------------------------------------------------------------------------


def append_to_sheet(
	path: str,
	sheet: str,
	data_json: str,
	step_number: int = 0,
	after_keyword: str = "",
) -> str:
	"""Append rows to an Excel sheet (.xlsx only). Writes full dataset in one bulk pass.
	data_json: JSON array of dicts where keys are source column names.
	Reads the sheet's own header row and maps source columns to destination columns by name.
	Columns present in sheet but missing in source are written as None.
	Columns present in source but absent from sheet are skipped (logged as warning).
	after_keyword: optional keyword to locate the right block before appending.
	Asks human for approval before writing."""
	_assert_output_path(path)

	records: list[dict] = json.loads(data_json)
	if not records:
		return "No data to append."

	wb = openpyxl.load_workbook(path)
	ws = wb[sheet]

	# Map destination sheet header → column index
	sheet_col_map = _get_sheet_headers(ws)
	if not sheet_col_map:
		wb.close()
		return f"Error: could not detect header row in sheet '{sheet}'."

	# Warn about source columns not in destination
	sample_keys = set(records[0].keys())
	unmatched = sample_keys - set(sheet_col_map.keys())
	if unmatched:
		logger.warning(
			"append_to_sheet: source columns not in sheet '%s' headers, will be skipped: %s",
			sheet,
			unmatched,
		)

	write_row = _find_write_row(ws, 1, ws.max_column, after_keyword)

	# Build preview using destination column order
	ordered_headers = sorted(sheet_col_map, key=lambda h: sheet_col_map[h])
	preview_rows_data = [[str(r.get(h, None)) for h in ordered_headers] for r in records[:5]]
	preview = _preview_rows(preview_rows_data, write_row, sheet)
	preview += f"\nDestination columns: {ordered_headers}"
	if unmatched:
		preview += f"\nSkipped source columns (not in sheet): {sorted(unmatched)}"

	status, feedback = verify_before_write(step_number, preview)
	if status == "retry":
		wb.close()
		return f"Write cancelled. Human feedback: {feedback}"

	# Bulk write — align each record to destination columns
	for i, record in enumerate(records):
		for col_name, col_idx in sheet_col_map.items():
			value = record.get(col_name, None)  # None if source has no value for this column
			ws.cell(row=write_row + i, column=col_idx, value=value)

	wb.save(path)
	wb.close()
	logger.info(
		"append_to_sheet: wrote %d rows to %s[%s] starting row %d",
		len(records),
		path,
		sheet,
		write_row,
	)
	return f"Appended {len(records)} rows to sheet '{sheet}' starting at row {write_row}. Columns matched: {sorted(sample_keys & set(sheet_col_map))}."


def create_sheet(path: str, sheet_name: str) -> str:
	"""Create a new sheet in the output workbook. Skips if sheet already exists."""
	_assert_output_path(path)

	wb = openpyxl.load_workbook(path)
	if sheet_name in wb.sheetnames:
		wb.close()
		return f"Sheet '{sheet_name}' already exists — skipped."
	wb.create_sheet(sheet_name)
	wb.save(path)
	wb.close()
	logger.info("create_sheet: '%s' in %s", sheet_name, path)
	return f"Created sheet '{sheet_name}'."


# ---------------------------------------------------------------------------
# Formula copy
# ---------------------------------------------------------------------------


def _col_letter_to_num(col: str) -> int:
	num = 0
	for c in col.upper():
		num = num * 26 + (ord(c) - ord("A") + 1)
	return num


def _col_num_to_letter(n: int) -> str:
	result = ""
	while n > 0:
		n, remainder = divmod(n - 1, 26)
		result = chr(65 + remainder) + result
	return result


def _offset_cell_refs(formula: str, col_offset: int, row_offset: int) -> str:
	"""Shift relative cell references in a formula string."""
	if col_offset == 0 and row_offset == 0:
		return formula

	def replace_ref(match):
		col_abs, col_str, row_abs, row_str = match.groups()
		new_col = (
			_col_num_to_letter(_col_letter_to_num(col_str) + col_offset)
			if not col_abs and col_offset
			else col_str
		)
		new_row = int(row_str) + row_offset if not row_abs and row_offset else int(row_str)
		return f"{col_abs}{new_col}{row_abs}{new_row}"

	return re.sub(r"(\$?)([A-Z]+)(\$?)(\d+)", replace_ref, formula)


def copy_formula(
	path: str,
	src_sheet: str,
	src_range: str,
	dst_sheet: str,
	dst_start_cell: str,
	substitutions_json: str = "{}",
	col_offset: int = 0,
	row_offset: int = 0,
	step_number: int = 0,
) -> str:
	"""Copy formulas from src_range to dst_sheet starting at dst_start_cell.
	Applies string substitutions (period changes) and col/row offsets in one bulk pass.
	substitutions_json: JSON dict e.g. '{"Jan-26": "Feb-26", "0126": "0226"}'.
	Asks human for approval before writing."""
	_assert_output_path(path)

	substitutions: dict[str, str] = json.loads(substitutions_json)

	wb = openpyxl.load_workbook(path, data_only=False)
	src_ws = wb[src_sheet]
	src_cells = list(src_ws[src_range])

	dst_match = re.match(r"([A-Z]+)(\d+)", dst_start_cell.upper())
	dst_col = _col_letter_to_num(dst_match.group(1))
	dst_row = int(dst_match.group(2))

	plan: list[tuple[str, str, str]] = []
	for r_idx, row in enumerate(src_cells):
		for c_idx, cell in enumerate(row):
			original = str(cell.value) if cell.value is not None else ""
			transformed = original
			for old, new in substitutions.items():
				transformed = transformed.replace(old, new)
			if transformed.startswith("="):
				transformed = _offset_cell_refs(transformed, col_offset, row_offset)
			dst_ref = f"{_col_num_to_letter(dst_col + c_idx)}{dst_row + r_idx}"
			plan.append((dst_ref, original, transformed))

	preview_lines = [
		f"Copying {len(plan)} cells: {src_sheet}!{src_range} → {dst_sheet}!{dst_start_cell}"
	]
	if substitutions:
		preview_lines.append(f"Substitutions: {substitutions}")
	if col_offset or row_offset:
		preview_lines.append(f"Offset: col={col_offset}, row={row_offset}")
	preview_lines.append("Sample (first 5):")
	for dst_ref, orig, trans in plan[:5]:
		preview_lines.append(f"  {dst_ref}: {orig!r} → {trans!r}")

	status, feedback = verify_before_write(step_number, "\n".join(preview_lines))
	if status == "retry":
		wb.close()
		return f"Formula copy cancelled. Human feedback: {feedback}"

	dst_ws = wb[dst_sheet]
	for dst_ref, _, transformed in plan:
		dst_ws[dst_ref] = transformed

	wb.save(path)
	wb.close()
	logger.info(
		"copy_formula: %d cells from %s!%s → %s!%s",
		len(plan),
		src_sheet,
		src_range,
		dst_sheet,
		dst_start_cell,
	)
	return f"Copied {len(plan)} formulas from {src_sheet}!{src_range} to {dst_sheet} starting at {dst_start_cell}."


# ---------------------------------------------------------------------------
# Computed columns — bulk Python computation, no LLM involvement
# ---------------------------------------------------------------------------


def _load_lookup_table(wb, sheet_name: str, key_col: str, val_col: str) -> dict:
	"""Load a sheet into a {key: value} dict, skipping metadata rows at the top.
	Header row is the first row with >= 2 non-empty cells."""
	ws = wb[sheet_name]
	headers: list = []
	header_row_idx = -1
	for i, row in enumerate(ws.iter_rows(max_row=20, values_only=True)):
		non_empty = [v for v in row if v is not None and str(v).strip()]
		if len(non_empty) >= 2:
			headers = list(row)
			header_row_idx = i + 1  # 1-based
			break
	if not headers:
		return {}

	def _idx(col_name: str) -> int:
		for j, h in enumerate(headers):
			if h is not None and str(h).strip() == col_name.strip():
				return j
		raise ValueError(
			f"Column '{col_name}' not found in sheet '{sheet_name}'. Headers: {headers}"
		)

	ki = _idx(key_col)
	vi = _idx(val_col)
	table = {}
	for row in ws.iter_rows(min_row=header_row_idx + 1, values_only=True):
		k = row[ki]
		v = row[vi]
		if k is not None:
			table[str(k)] = v
	return table


def _format_date_value(val, fmt: str) -> str:
	"""Format a cell value (datetime, date, or string) using strftime fmt.
	Supports '%-m' on Unix for month without leading zero."""
	if val is None:
		return ""
	if isinstance(val, datetime):
		dt = val
	elif hasattr(val, "year"):  # date object
		dt = datetime(val.year, val.month, val.day)
	else:
		s = str(val).strip()
		for pat in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
			try:
				dt = datetime.strptime(s, pat)
				break
			except ValueError:
				continue
		else:
			return s  # can't parse — return as-is
	try:
		return dt.strftime(fmt)
	except ValueError:
		# Windows doesn't support %-m; fall back to manual stripping
		result = dt.strftime(fmt.replace("%-m", "%m").replace("%-d", "%d"))
		result = re.sub(r"\b0(\d)", r"\1", result)  # strip leading zeros
		return result


def _read_src_headers_and_sample(
	source_path: str, source_sheet: str, n: int = 5
) -> tuple[list, list]:
	"""Return (headers, sample_rows) reading only the first n data rows — fast for large files."""
	src_p = Path(source_path)
	if src_p.suffix.lower() in (".xls", ".xml") or _is_spreadsheetml(source_path):
		raw_rows = _read_spreadsheetml_sheet(source_path, source_sheet)
		if not raw_rows:
			return [], []
		headers = [str(h) if h else f"col_{i}" for i, h in enumerate(raw_rows[0])]
		sample = []
		for row in raw_rows[1 : n + 1]:
			padded = (row + [""] * len(headers))[: len(headers)]
			if any(v not in ("", None) for v in padded):
				sample.append(list(padded))
	else:
		wb = openpyxl.load_workbook(source_path, data_only=True, read_only=True)
		ws = wb[source_sheet]
		raw = []
		for i, row in enumerate(ws.iter_rows(values_only=True)):
			raw.append(row)
			if i > n:
				break
		wb.close()
		if not raw:
			return [], []
		headers = [str(h) if h is not None else f"col_{i}" for i, h in enumerate(raw[0])]
		sample = [list(r) for r in raw[1 : n + 1] if any(v is not None for v in r)]
	return headers, sample


def _read_all_src_data(source_path: str, source_sheet: str, src_headers: list) -> list:
	"""Read all data rows from source (already know headers)."""
	src_p = Path(source_path)
	if src_p.suffix.lower() in (".xls", ".xml") or _is_spreadsheetml(source_path):
		raw_rows = _read_spreadsheetml_sheet(source_path, source_sheet)
		result = []
		for row in raw_rows[1:]:
			padded = (row + [""] * len(src_headers))[: len(src_headers)]
			if any(v not in ("", None) for v in padded):
				result.append(list(padded))
		return result
	else:
		wb = openpyxl.load_workbook(source_path, data_only=True, read_only=True)
		ws = wb[source_sheet]
		raw = list(ws.iter_rows(values_only=True))
		wb.close()
		return [list(r) for r in raw[1:] if any(v is not None for v in r)]


def add_computed_columns(
	output_path: str,
	output_sheet: str,
	columns_json: str,
	source_path: str = "",
	source_sheet: str = "",
	step_number: int = 0,
) -> str:
	"""Add computed columns to an output sheet entirely in Python (no LLM row processing).

	If source_path + source_sheet are provided, reads base data from that file and writes
	everything (original columns + new columns) into output_sheet — use this when the
	output sheet is empty or needs to be populated from an input file.
	Otherwise, reads existing rows from output_sheet and appends new columns in-place.

	columns_json — JSON array of column specs:
	  {"name": "col_name", "expr": "<type>", "args": {...}}

	Supported expr types:
	  "concat"       — join fields; args: {fields, sep, date_cols: {col: strftime_fmt}}
	  "vlookup"      — args: {key_col, lookup_sheet, lookup_key_col, lookup_value_col, default}
	  "multiply"     — args: {col_a, col_b}
	  "flag_equals"  — args: {col, value}   → "YES"/"NO"
	  "flag_empty"   — args: {col}          → "YES"/"NO"

	vlookup sheets must exist in the output file.
	Asks human for approval (using a small sample) BEFORE reading all rows."""
	_assert_output_path(output_path)

	column_specs: list[dict] = json.loads(columns_json)
	new_col_names = [s["name"] for s in column_specs]
	reading_from_source = bool(source_path and source_sheet)

	# --- Step 1: read headers + small sample only (fast) ---
	if reading_from_source:
		src_headers, sample_rows = _read_src_headers_and_sample(source_path, source_sheet)
		if not src_headers:
			return f"Source sheet '{source_sheet}' in {source_path} is empty."
		# Get total row count cheaply
		wb_tmp = openpyxl.load_workbook(source_path, data_only=True, read_only=True)
		total_rows = wb_tmp[source_sheet].max_row - 1
		wb_tmp.close()
	else:
		wb_tmp = openpyxl.load_workbook(output_path, data_only=True, read_only=True)
		ws_tmp = wb_tmp[output_sheet]
		raw_tmp = list(ws_tmp.iter_rows(max_row=6, values_only=True))
		total_rows = ws_tmp.max_row - 1
		wb_tmp.close()
		if not raw_tmp:
			return f"Output sheet '{output_sheet}' is empty. Pass source_path/source_sheet to populate it first."
		src_headers = [str(h) if h is not None else f"col_{i}" for i, h in enumerate(raw_tmp[0])]
		sample_rows = [list(r) for r in raw_tmp[1:] if any(v is not None for v in r)]

	# --- Step 2: load lookups (small reference sheets — fast) ---
	out_wb_read = openpyxl.load_workbook(output_path, data_only=True, read_only=True)
	lookups: dict[tuple, dict] = {}
	for spec in column_specs:
		if spec["expr"] == "vlookup":
			a = spec["args"]
			key = (a["lookup_sheet"], a["lookup_key_col"], a["lookup_value_col"])
			if key not in lookups:
				lookups[key] = _load_lookup_table(out_wb_read, *key)
				logger.debug("Loaded lookup '%s': %d entries", a["lookup_sheet"], len(lookups[key]))
	out_wb_read.close()

	def compute_row(row_vals: dict) -> list:
		computed: dict = {}
		for spec in column_specs:
			name = spec["name"]
			expr = spec["expr"]
			args = spec["args"]

			if expr == "concat":
				parts = []
				for field in args["fields"]:
					val = computed.get(field, row_vals.get(field))
					date_fmt = args.get("date_cols", {}).get(field)
					if date_fmt:
						val = _format_date_value(val, date_fmt)
					else:
						val = "" if val is None else str(val)
					parts.append(val)
				result = args.get("sep", "-").join(parts)

			elif expr == "vlookup":
				key_val = computed.get(args["key_col"], row_vals.get(args["key_col"]))
				lk_key = (args["lookup_sheet"], args["lookup_key_col"], args["lookup_value_col"])
				result = lookups[lk_key].get(
					str(key_val) if key_val is not None else "", args.get("default", "")
				)

			elif expr == "multiply":
				a_val = computed.get(args["col_a"], row_vals.get(args["col_a"]))
				b_val = computed.get(args["col_b"], row_vals.get(args["col_b"]))
				try:
					result = float(a_val or 0) * float(b_val or 0)
				except (ValueError, TypeError):
					result = ""

			elif expr == "flag_equals":
				val = computed.get(args["col"], row_vals.get(args["col"]))
				result = (
					"YES" if str(val or "").strip().upper() == str(args["value"]).upper() else "NO"
				)

			elif expr == "flag_empty":
				val = computed.get(args["col"], row_vals.get(args["col"]))
				result = (
					"YES"
					if (val is None or str(val).strip() == "" or val == args.get("default", ""))
					else "NO"
				)

			else:
				result = f"Unknown expr: {expr}"

			computed[name] = result
		return [computed[n] for n in new_col_names]

	# --- Step 3: preview using sample rows only, then ask approval ---
	sample_new_values = []
	for raw_row in sample_rows[:3]:
		padded = (raw_row + [None] * len(src_headers))[: len(src_headers)]
		sample_new_values.append(compute_row(dict(zip(src_headers, padded))))

	preview_lines = [
		f"add_computed_columns: {output_sheet} | {total_rows} rows | "
		f"{'from ' + source_sheet if reading_from_source else 'in-place'}",
		f"New columns: {new_col_names}",
		"Sample (first 3 rows):",
	]
	for i, new_vals in enumerate(sample_new_values):
		preview_lines.append(f"  Row {i + 2}: {dict(zip(new_col_names, new_vals))}")

	status, feedback = verify_before_write(step_number, "\n".join(preview_lines))
	if status == "retry":
		return f"add_computed_columns cancelled. Human feedback: {feedback}"

	# --- Step 4: read ALL rows and compute (only runs after approval) ---
	if reading_from_source:
		logger.info(
			"add_computed_columns: reading %d rows from %s[%s]",
			total_rows,
			source_path,
			source_sheet,
		)
		src_data = _read_all_src_data(source_path, source_sheet, src_headers)
	else:
		logger.info(
			"add_computed_columns: reading %d rows from output sheet %s",
			total_rows,
			output_sheet,
		)
		wb_full = openpyxl.load_workbook(output_path, data_only=True, read_only=True)
		raw_full = list(wb_full[output_sheet].iter_rows(values_only=True))
		wb_full.close()
		src_data = [list(r) for r in raw_full[1:] if any(v is not None for v in r)]

	all_new_values: list[list] = []
	for raw_row in src_data:
		padded = (raw_row + [None] * len(src_headers))[: len(src_headers)]
		all_new_values.append(compute_row(dict(zip(src_headers, padded))))

	# --- Step 5: write ---
	out_wb = openpyxl.load_workbook(output_path)
	out_ws = out_wb[output_sheet]

	if reading_from_source:
		all_headers = src_headers + new_col_names
		for ci, h in enumerate(all_headers, start=1):
			out_ws.cell(row=1, column=ci, value=h)
		for ri, (raw_row, new_vals) in enumerate(zip(src_data, all_new_values), start=2):
			padded = (raw_row + [None] * len(src_headers))[: len(src_headers)]
			for ci, v in enumerate(padded + new_vals, start=1):
				out_ws.cell(row=ri, column=ci, value=v)
	else:
		next_col = out_ws.max_column + 1
		for ci, h in enumerate(new_col_names, start=next_col):
			out_ws.cell(row=1, column=ci, value=h)
		for ri, new_vals in enumerate(all_new_values, start=2):
			for ci, v in enumerate(new_vals, start=next_col):
				out_ws.cell(row=ri, column=ci, value=v)

	out_wb.save(output_path)
	out_wb.close()
	logger.info(
		"add_computed_columns: wrote %d rows, %d new cols to %s[%s]",
		len(src_data),
		len(new_col_names),
		output_path,
		output_sheet,
	)
	return (
		f"Added {len(new_col_names)} computed columns to '{output_sheet}': {new_col_names}. "
		f"Rows written: {len(src_data)}."
	)


# ---------------------------------------------------------------------------
# Aggregation / pivot
# ---------------------------------------------------------------------------


def aggregate_sheet(
	source_path: str,
	source_sheet: str,
	output_path: str,
	output_sheet: str,
	group_by_json: str,
	agg_cols_json: str,
	step_number: int = 0,
	filter_col: str = "",
	filter_value: str = "",
) -> str:
	"""Group rows from source_sheet by key columns and write aggregated totals to output_sheet.

	group_by_json  — JSON array of column names to group by, e.g. ["COUNTRY", "CURRENCYCODE"]
	agg_cols_json  — JSON array of {col, func} dicts, e.g.:
	                 [{"col": "USD Sales Amount", "func": "sum"},
	                  {"col": "USD Tax Amount",   "func": "sum"},
	                  {"col": "DOCUMENTID",       "func": "count"}]
	                 Supported funcs: "sum", "count"
	filter_col     — optional column name to pre-filter rows before aggregating
	filter_value   — keep only rows where filter_col == filter_value (case-insensitive)

	Reads source from source_path (any readable file).
	Writes result to output_path (must be inside fyra/output/).
	Asks human for approval before writing."""
	_assert_output_path(output_path)

	group_by: list[str] = json.loads(group_by_json)
	agg_cols: list[dict] = json.loads(agg_cols_json)

	# --- read source ---
	src_p = Path(source_path)
	if src_p.suffix.lower() in (".xls", ".xml") or _is_spreadsheetml(source_path):
		raw_rows = _read_spreadsheetml_sheet(source_path, source_sheet)
		if not raw_rows:
			return f"Source sheet '{source_sheet}' is empty."
		src_headers = [str(h) if h else f"col_{i}" for i, h in enumerate(raw_rows[0])]
		data_rows = [list(r) for r in raw_rows[1:]]
	else:
		wb = openpyxl.load_workbook(source_path, data_only=True, read_only=True)
		ws = wb[source_sheet]
		raw = list(ws.iter_rows(values_only=True))
		wb.close()
		if not raw:
			return f"Source sheet '{source_sheet}' is empty."
		src_headers = [str(h) if h is not None else f"col_{i}" for i, h in enumerate(raw[0])]
		data_rows = [list(r) for r in raw[1:]]

	# validate columns exist
	missing = [c for c in group_by if c not in src_headers]
	missing += [a["col"] for a in agg_cols if a["col"] not in src_headers]
	if missing:
		return (
			f"Columns not found in '{source_sheet}': {missing}. "
			f"Available: {src_headers}"
		)

	gi = [src_headers.index(c) for c in group_by]
	ai = [(src_headers.index(a["col"]), a["func"]) for a in agg_cols]
	fi = src_headers.index(filter_col) if filter_col and filter_col in src_headers else -1

	# --- aggregate ---
	buckets: dict[tuple, list] = {}
	key_order: list[tuple] = []

	for row in data_rows:
		if len(row) < len(src_headers):
			row += [None] * (len(src_headers) - len(row))

		if fi >= 0 and filter_value:
			if str(row[fi] or "").strip().lower() != filter_value.lower():
				continue

		key = tuple(str(row[i] or "") for i in gi)
		if key not in buckets:
			buckets[key] = [0] * len(ai)
			key_order.append(key)

		for slot, (idx, func) in enumerate(ai):
			raw_val = row[idx]
			if func == "sum":
				try:
					buckets[key][slot] += float(raw_val or 0)
				except (ValueError, TypeError):
					pass
			elif func == "count":
				if raw_val is not None and str(raw_val).strip() != "":
					buckets[key][slot] += 1

	total_groups = len(key_order)
	if total_groups == 0:
		msg = f"No rows matched"
		if filter_col:
			msg += f" filter {filter_col}={filter_value!r}"
		return msg + f" in '{source_sheet}'."

	out_headers = group_by + [a["col"] for a in agg_cols]

	# --- preview ---
	preview_lines = [
		f"aggregate_sheet: {source_sheet} → {output_sheet} | "
		f"{total_groups} groups from {len(data_rows)} source rows",
		f"Group by: {group_by}",
		"Aggregations: " + str([f"{a['col']} ({a['func']})" for a in agg_cols]),
	]
	if filter_col:
		preview_lines.append(f"Filter: {filter_col} = {filter_value!r}")
	preview_lines.append("Sample (first 5 groups):")
	for key in key_order[:5]:
		vals = [round(v, 6) for v in buckets[key]]
		preview_lines.append(f"  {dict(zip(out_headers, list(key) + vals))}")

	status, feedback = verify_before_write(step_number, "\n".join(preview_lines))
	if status == "retry":
		return f"aggregate_sheet cancelled. Human feedback: {feedback}"

	# --- write ---
	out_wb = openpyxl.load_workbook(output_path)
	if output_sheet not in out_wb.sheetnames:
		out_wb.create_sheet(output_sheet)
	out_ws = out_wb[output_sheet]

	# clear any existing content
	for row in out_ws.iter_rows():
		for cell in row:
			cell.value = None

	for ci, h in enumerate(out_headers, start=1):
		out_ws.cell(row=1, column=ci, value=h)

	for ri, key in enumerate(key_order, start=2):
		row_out = list(key) + buckets[key]
		for ci, v in enumerate(row_out, start=1):
			out_ws.cell(row=ri, column=ci, value=v)

	out_wb.save(output_path)
	out_wb.close()
	logger.info(
		"aggregate_sheet: %d groups written to %s[%s]",
		total_groups, output_path, output_sheet,
	)
	return (
		f"Wrote {total_groups} aggregated rows to '{output_sheet}'. "
		f"Columns: {out_headers}."
	)


# ---------------------------------------------------------------------------
# XML / HTML report files (NetSuite FAM reports)
# ---------------------------------------------------------------------------


def read_xml(path: str) -> str:
	"""Parse a SpreadsheetML or plain XML file and return sheet names and row counts.
	For SpreadsheetML files, use read_excel() to get actual data.
	Returns a JSON summary dict."""
	logger.debug("read_xml: %s", path)

	if _is_spreadsheetml(path):
		sheets = _spreadsheetml_sheets(path)
		summary = {
			"format": "SpreadsheetML",
			"sheets": sheets,
			"note": "Use read_excel() with a sheet name to read data.",
		}
		return json.dumps(summary)

	# Plain XML — return tag summary
	parser = etree.XMLParser(recover=True)
	tree = etree.parse(path, parser)
	root = tree.getroot()
	tag = root.tag.split("}")[-1]
	children = [c.tag.split("}")[-1] for c in root][:10]
	return json.dumps({"root_tag": tag, "top_children": children})


def extract_html_tables_from_xml(path: str) -> str:
	"""Extract all HTML <table> elements from a NetSuite XML report file.
	Handles NetSuite report structure: metadata rows → column headers → data rows
	with optional single-cell section headers (e.g. 'Year 2026 - Electronic Devices').

	Each data row gets a '_section' field indicating the last section header seen.
	Returns a JSON array of tables; each table is a list of row dicts."""
	logger.debug("extract_html_tables_from_xml: %s", path)

	with open(path, "rb") as f:
		raw = f.read()

	soup = BeautifulSoup(raw, "html.parser")
	tables_out = []

	for table in soup.find_all("table"):
		all_rows = table.find_all("tr")
		if not all_rows:
			continue

		# Find the first row with >= 3 non-empty cells — that is the column header row.
		# Rows before it are report title/metadata and are skipped.
		headers: list[str] = []
		header_row_idx = -1
		for i, tr in enumerate(all_rows):
			cells = [td.get_text(strip=True) for td in tr.find_all(["th", "td"])]
			non_empty = [c for c in cells if c]
			if len(non_empty) >= 3:
				headers = cells
				header_row_idx = i
				break

		if not headers or header_row_idx < 0:
			continue

		table_data: list[dict] = []
		current_section = ""

		for tr in all_rows[header_row_idx + 1 :]:
			cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
			non_empty = [c for c in cells if c]

			# Single-cell row = section header (e.g. "Year 2026 - Electronic Devices")
			if len(non_empty) == 1:
				current_section = non_empty[0]
				continue

			if not non_empty:
				continue

			# Pad/trim to match header count and build row dict
			cells = (cells + [""] * len(headers))[: len(headers)]
			row = dict(zip(headers, cells))
			if current_section:
				row["_section"] = current_section
			table_data.append(row)

		if table_data:
			tables_out.append(table_data)

	logger.debug(
		"extract_html_tables_from_xml: %d tables, total rows: %d",
		len(tables_out),
		sum(len(t) for t in tables_out),
	)
	return json.dumps(tables_out)
