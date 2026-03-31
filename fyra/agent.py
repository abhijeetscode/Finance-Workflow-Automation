"""
SOP Agent — orchestrates step-by-step execution with human verification.
"""

import json
import logging
import os
from pathlib import Path

import dspy

from signatures import BuildFormulaSubstitutions, ExecuteSOPStep, InferFileRole, SOPStep
from sop_parser import SOPParser, read_docx
from human_loop import gather_file_context, verify_step_completion
from tools import (
	read_excel,
	list_sheets,
	append_to_sheet,
	create_sheet,
	copy_formula,
	read_xml,
	extract_html_tables_from_xml,
	add_computed_columns,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Formula substitution helper (DSPy)
# ---------------------------------------------------------------------------


class FormulaSubstitutionBuilder(dspy.Module):
	def __init__(self):
		self.build = dspy.ChainOfThought(BuildFormulaSubstitutions)

	def forward(self, source_period: str, target_period: str) -> dict[str, str]:
		result = self.build(source_period=source_period, target_period=target_period)
		logger.debug("Formula substitutions: %s", result.substitutions)
		return result.substitutions


# ---------------------------------------------------------------------------
# Tool wrappers — inject step_number at runtime
# ---------------------------------------------------------------------------


def make_tools(step_number_ref: list[int]) -> list:
	"""
	Returns tool functions for dspy.ReAct.
	step_number_ref is a mutable list[int] so the current step number
	can be updated by the agent loop without recreating tools each step.
	"""

	def read_excel_tool(path: str, sheet: str) -> str:
		"""Read schema and sample rows from an Excel sheet (.xls or .xlsx).
		Returns JSON with total_rows, columns list, and up to 25 sample rows.
		Use total_rows and columns to understand the data structure — do NOT
		attempt to retrieve all rows, as large files will exceed the API limit."""
		return read_excel(path, sheet, max_rows=25)

	def list_sheets_tool(path: str) -> str:
		"""List all sheet names in an Excel file. Returns JSON array."""
		return list_sheets(path)

	def append_to_sheet_tool(
		path: str,
		sheet: str,
		data_json: str,
		after_keyword: str = "",
		start_col: int = 1,
	) -> str:
		"""Append rows to an Excel sheet (output copy only). data_json is a JSON array of rows.
		after_keyword: optional header to locate the right block before appending.
		Asks human for approval before writing."""
		return append_to_sheet(path, sheet, data_json, step_number_ref[0], after_keyword, start_col)

	def create_sheet_tool(path: str, sheet_name: str) -> str:
		"""Create a new sheet in the output workbook."""
		return create_sheet(path, sheet_name)

	def copy_formula_tool(
		path: str,
		src_sheet: str,
		src_range: str,
		dst_sheet: str,
		dst_start_cell: str,
		substitutions_json: str = "{}",
		col_offset: int = 0,
		row_offset: int = 0,
	) -> str:
		"""Copy formulas from src_range to dst_sheet with period substitutions and cell offsets.
		substitutions_json: JSON dict mapping old string to new string (e.g. period name variants).
		Asks human for approval before writing."""
		return copy_formula(
			path,
			src_sheet,
			src_range,
			dst_sheet,
			dst_start_cell,
			substitutions_json,
			col_offset,
			row_offset,
			step_number_ref[0],
		)

	def build_formula_substitutions_tool(source_period: str, target_period: str) -> str:
		"""Build all date/period format variants for formula substitution.
		Returns a JSON dict covering all format variants found in Excel formulas and sheet names."""
		subs = FormulaSubstitutionBuilder().forward(source_period, target_period)
		return json.dumps(subs)

	def read_xml_tool(path: str) -> str:
		"""Parse an XML file and return its content as a JSON dict."""
		return read_xml(path)

	def extract_html_tables_tool(path: str) -> str:
		"""Extract all HTML tables embedded inside an XML file.
		Returns a JSON array of tables (each table is a list of row dicts)."""
		return extract_html_tables_from_xml(path)

	def add_computed_columns_tool(
		output_path: str,
		output_sheet: str,
		columns_json: str,
		source_path: str = "",
		source_sheet: str = "",
	) -> str:
		"""Add computed columns to an output sheet entirely in Python — no row data passes through the LLM.
		Use this instead of read_excel + manual computation for any sheet with more than a few hundred rows.

		source_path + source_sheet: if the output sheet is empty, provide the input file to populate it from.
		  The tool will copy all base rows AND append the new computed columns in one pass.
		  If omitted, adds new columns to existing rows in output_sheet in-place.

		columns_json — JSON array of column specs, each:
		  {"name": "col_name", "expr": "<type>", "args": {...}}

		Supported expr types:
		  "concat"      — join fields; args: {"fields": ["COL_A","COL_B"], "sep": "-",
		                    "date_cols": {"COL_A": "%-m-%d-%Y"}}
		  "vlookup"     — args: {"key_col": "MY_COL", "lookup_sheet": "SheetName",
		                    "lookup_key_col": "Key", "lookup_value_col": "Value", "default": ""}
		  "multiply"    — args: {"col_a": "COL1", "col_b": "COL2"}
		  "flag_equals" — args: {"col": "COL1", "value": "US"}  → "YES"/"NO"
		  "flag_empty"  — args: {"col": "COL1"}                 → "YES"/"NO"

		vlookup sheets must be in the output file.
		FX rate key format in NS Daily FX Rates is '%-m-%d-%Y-CURRENCYCODE' (e.g. '12-15-2025-USD')."""
		return add_computed_columns(
			output_path,
			output_sheet,
			columns_json,
			source_path,
			source_sheet,
			step_number_ref[0],
		)

	return [
		read_excel_tool,
		list_sheets_tool,
		append_to_sheet_tool,
		create_sheet_tool,
		copy_formula_tool,
		build_formula_substitutions_tool,
		read_xml_tool,
		extract_html_tables_tool,
		add_computed_columns_tool,
	]


# ---------------------------------------------------------------------------
# Main agent entry point
# ---------------------------------------------------------------------------


def run_agent(
	sop_path: Path,
	output_path: Path,
	input_files: list[Path],
	run_id: str,
) -> None:
	logger.info("Configuring DSPy LM")
	model = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
	lm = dspy.LM(f"anthropic/{model}", max_tokens=8192)
	dspy.configure(lm=lm)

	# Step 1 — auto-infer file roles from sample data, then confirm with human
	infer = dspy.Predict(InferFileRole)
	inferred: dict[str, str] = {}
	for path in input_files:
		try:
			sample_json = read_excel(str(path), next(iter(
				__import__("json").loads(list_sheets(str(path)))
			)), max_rows=5)
			result = infer(file_name=path.name, sample_data=sample_json)
			inferred[path.name] = result.description
			logger.info("Inferred role for %s: %s", path.name, result.description)
		except Exception as exc:
			logger.warning("Could not infer role for %s: %s", path.name, exc)

	file_context = gather_file_context(input_files, inferred=inferred)
	file_context_json = json.dumps(file_context)
	input_paths_json = json.dumps([str(p) for p in input_files])

	# Step 2 — parse SOP into ordered steps
	logger.info("Parsing SOP: %s", sop_path)
	sop_text = read_docx(str(sop_path))
	steps: list[SOPStep] = SOPParser().forward(sop_text, file_context_json)
	logger.info("SOP parsed into %d steps", len(steps))

	# Step 3 — execute each step sequentially
	step_number_ref = [0]  # mutable ref so tools always see current step
	tools = make_tools(step_number_ref)
	executor = dspy.ReAct(ExecuteSOPStep, tools=tools, max_iters=20)

	for step in steps:
		step_number_ref[0] = step.step_number
		logger.info("=" * 50)
		logger.info("Starting step %d: %s", step.step_number, step.description)
		print(f"\n{'=' * 60}")
		print(f"[Step {step.step_number}] {step.description}")
		print(f"{'=' * 60}")

		while True:
			result = executor(
				step_description=step.description,
				file_context=file_context_json,
				output_file_path=str(output_path),
				input_file_paths=input_paths_json,
			)

			status, feedback = verify_step_completion(step.step_number, result.result)

			if status == "ok":
				logger.info("Step %d complete.", step.step_number)
				break

			# Retry — append feedback to step description so the LM adjusts
			logger.info("Retrying step %d. Feedback: %s", step.step_number, feedback)
			step = SOPStep(
				step_number=step.step_number,
				description=step.description
				+ f"\n\n[Retry] Human feedback from previous attempt: {feedback}",
				input_files=step.input_files,
				expected_output=step.expected_output,
			)

	logger.info("All steps complete. Output: %s", output_path)
	print(f"\nDone. Output file: {output_path}")
