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
    target = next(
        (ws for ws in worksheets if ws.get(f"{{{_SS_NS}}}Name") == sheet_name), None
    )
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

def read_excel(path: str, sheet: str) -> str:
    """Read all data from an Excel sheet (.xls SpreadsheetML or .xlsx).
    Returns a JSON array of dicts where the first row is used as column headers.
    Rows where all values are empty are skipped."""
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
        return json.dumps([])

    headers = [str(h) if h else f"col_{i}" for i, h in enumerate(raw_rows[0])]
    records = []
    for row in raw_rows[1:]:
        padded = (row + [""] * len(headers))[: len(headers)]
        if all(v == "" or v is None for v in padded):
            continue
        records.append(dict(zip(headers, padded)))

    logger.debug("read_excel: %d records returned", len(records))
    return json.dumps(records)


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
        logger.warning("append_to_sheet: source columns not in sheet '%s' headers, will be skipped: %s", sheet, unmatched)

    write_row = _find_write_row(ws, 1, ws.max_column, after_keyword)

    # Build preview using destination column order
    ordered_headers = sorted(sheet_col_map, key=lambda h: sheet_col_map[h])
    preview_rows_data = [
        [str(r.get(h, None)) for h in ordered_headers]
        for r in records[:5]
    ]
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
    logger.info("append_to_sheet: wrote %d rows to %s[%s] starting row %d", len(records), path, sheet, write_row)
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
        new_col = _col_num_to_letter(_col_letter_to_num(col_str) + col_offset) if not col_abs and col_offset else col_str
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

    preview_lines = [f"Copying {len(plan)} cells: {src_sheet}!{src_range} → {dst_sheet}!{dst_start_cell}"]
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
    logger.info("copy_formula: %d cells from %s!%s → %s!%s", len(plan), src_sheet, src_range, dst_sheet, dst_start_cell)
    return f"Copied {len(plan)} formulas from {src_sheet}!{src_range} to {dst_sheet} starting at {dst_start_cell}."


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

        for tr in all_rows[header_row_idx + 1:]:
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

    logger.debug("extract_html_tables_from_xml: %d tables, total rows: %d",
                 len(tables_out), sum(len(t) for t in tables_out))
    return json.dumps(tables_out)
