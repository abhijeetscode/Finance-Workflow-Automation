import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import dspy
import pandas as pd
import structlog
from dotenv import load_dotenv
from pydantic import BaseModel
from tabulate import tabulate

from code_gen_models import (
	BusinessLogicSpec,
	ColumnMatch,
	InputMapping,
	InputSheetSignature,
	OutputSheetSpec,
	SemanticValidation,
	SheetMatch,
	TransformationRule,
)

load_dotenv()

logger = structlog.get_logger("CodeGenTools")


# ── Observation Result ───────────────────────────────────────────────────────


class ObservationResult(BaseModel):
	tool: str
	success: bool
	output: str
	data: Any = None


# ── DSPy Signatures ─────────────────────────────────────────────────────────


class InputSchemaSignature(dspy.Signature):
	"""Understand the semantic meaning of each sheet and column in an Excel file
	used for vendor payment processing. Focus on WHAT the data means, not what
	it's called. Identify patterns that would help recognize the same data even
	if sheet/column names change."""

	working_input_sample: str = dspy.InputField(
		desc="Tabular sample from the working input file showing all sheets with row data"
	)
	source_code: str = dspy.InputField(
		desc="Source code of the processing script that consumes this input"
	)
	input_sheets: list[InputSheetSignature] = dspy.OutputField(
		desc="Semantic understanding of each input sheet and its columns"
	)


class OutputSchemaSignature(dspy.Signature):
	"""Understand the structure of the expected output file for vendor payment
	processing. Identify each sheet, its columns, and what data goes where."""

	output_sample: str = dspy.InputField(
		desc="Tabular sample from the expected output file showing all sheets with row data"
	)
	output_sheets: list[OutputSheetSpec] = dspy.OutputField(
		desc="Structure of each output sheet including columns and their source/transformation"
	)


class TransformationSignature(dspy.Signature):
	"""Given the semantic understanding of input and output, plus the source code,
	extract the business rules that transform input into output. Also derive
	semantic validation checks from the rules."""

	input_schema: str = dspy.InputField(
		desc="JSON string of input sheet signatures with semantic roles and column meanings"
	)
	output_schema: str = dspy.InputField(
		desc="JSON string of output sheet specs with columns and their sources"
	)
	source_code: str = dspy.InputField(desc="Source code of the processing script")
	transformation_rules: list[TransformationRule] = dspy.OutputField(
		desc="Business rules that transform input into output"
	)
	semantic_validations: list[SemanticValidation] = dspy.OutputField(
		desc="Validation checks derived from the transformation rules"
	)


class SheetIdentificationSignature(dspy.Signature):
	"""Identify which sheet in a new input file corresponds to which semantic role
	from the business logic spec. Match by CONTENT (column names, data types,
	data patterns, row count), not by sheet name.

	Score each match using this rubric:
	- Column overlap >= 80% with spec: +0.35
	- Data types match spec: +0.25
	- Data patterns match spec: +0.20
	- Row count is plausible: +0.10
	- Sheet name similar or exact match: +0.10
	"""

	input_sheet_signatures: str = dspy.InputField(
		desc="JSON string of expected input sheet signatures with semantic roles and identifying traits"
	)
	new_file_sample: str = dspy.InputField(
		desc="Tabular sample from the new input file showing all sheets"
	)
	sheet_matches: list[SheetMatch] = dspy.OutputField(
		desc="Mapping of semantic roles to actual sheet names with confidence scores and breakdown"
	)


class ColumnMappingSignature(dspy.Signature):
	"""For a single identified sheet, map its columns to the expected semantic columns.
	Match by name similarity, data type, and value patterns.

	Score each match using this rubric:
	- Exact name match: +0.40
	- Semantic name match: +0.25
	- Data type matches spec: +0.20
	- Value pattern matches spec: +0.15
	"""

	expected_columns: str = dspy.InputField(
		desc="JSON string of expected columns for this sheet with semantic names and patterns"
	)
	actual_sheet_sample: str = dspy.InputField(
		desc="Tabular sample from the actual sheet in the new file"
	)
	column_matches: list[ColumnMatch] = dspy.OutputField(
		desc="Mapping of semantic column names to actual column names with confidence and breakdown"
	)


class CodeGenerationSignature(dspy.Signature):
	"""Generate a Python script that transforms an input Excel file into the expected
	output format. The script should:
	- Accept input_file and output_dir as sys.argv[1] and sys.argv[2]
	- Use pandas for data manipulation
	- Apply all transformation rules from the business logic spec
	- Use the actual sheet/column names from the input mapping (not the semantic names)
	- Write output as both .xlsx (audit) and .csv (Bill.com upload)
	"""

	business_logic_spec: str = dspy.InputField(desc="JSON string of the full business logic spec")
	input_mapping: str = dspy.InputField(
		desc="JSON string mapping semantic roles to actual sheet/column names in the new file"
	)
	new_file_sample: str = dspy.InputField(desc="Tabular sample from the new input file")
	reference_code: str = dspy.InputField(
		desc="Reference Python script showing working transformation patterns"
	)
	user_instructions: str = dspy.InputField(
		desc="Additional instructions from the user, or 'none' if no extra context"
	)
	python_script: str = dspy.OutputField(
		desc="Complete Python script that transforms input to expected output format"
	)


class CodeFixSignature(dspy.Signature):
	"""Fix a Python script that failed during execution.

	You are given:
	- The script that failed
	- The error message from stdout/stderr
	- The business logic spec (semantic roles)
	- The input mapping (semantic role -> ACTUAL sheet/column names in the file)

	CRITICAL: The input_mapping tells you the REAL sheet and column names in the file.
	Always use the actual names from the mapping, NOT the semantic names from the spec.
	For example, if the mapping says semantic_role="Vendor Name Standardization Mapping"
	maps to actual_sheet_name="Abhijeet", the script must use "Abhijeet" as the sheet name.
	"""

	python_script: str = dspy.InputField(desc="The Python script that failed")
	error_message: str = dspy.InputField(desc="Error output from the failed execution")
	business_logic_spec: str = dspy.InputField(
		desc="JSON string of the business logic spec (semantic names)"
	)
	input_mapping: str = dspy.InputField(
		desc="JSON string mapping semantic roles to ACTUAL sheet/column names in the input file. Always use these actual names in the fixed script."
	)
	fixed_python_script: str = dspy.OutputField(desc="Corrected Python script")


# ── Helper Functions ─────────────────────────────────────────────────────────


def get_file_sample(file_path: str, nrows: int = 5) -> str:
	xl = pd.ExcelFile(file_path)
	result = []
	for sheet_name in xl.sheet_names:
		df = pd.read_excel(xl, sheet_name=sheet_name)
		data_sample = df.head(nrows)
		result.append(f"Sheet: {sheet_name} ({len(df)} rows, {len(df.columns)} columns)")
		result.append(tabulate(data_sample, headers="keys", tablefmt="pipe", showindex=False))
		result.append("")
	return "\n".join(result)


def get_source_code() -> str:
	return Path("./code/process.py").read_text()


def get_lm() -> dspy.LM:
	model = os.getenv("LM_MODEL", "claude-haiku-4-5")
	return dspy.LM(
		f"anthropic/{model}",
		temperature=0.0,
		api_key=os.getenv("ANTHROPIC_API_KEY"),
	)


# ── Tool Functions ───────────────────────────────────────────────────────────


def tool_learn_business_logic(
	working_input_path: str = "./inputs/[Simple] Bill processing.xlsx",
	output_path: str = "./outputs/Vendor Payment Processing Audit 02.04.26.xlsx",
) -> ObservationResult:
	"""
	learn_business_logic() -> str
	Run the learn phase: extract BusinessLogicSpec from working input, process.py, and output.
	Returns:
	    Summary of extracted business logic spec.
	"""
	cache_path = Path("./business_logic_spec.json")
	if cache_path.exists():
		logger.info("Loading cached BusinessLogicSpec from %s", cache_path)
		spec = BusinessLogicSpec.model_validate_json(cache_path.read_text())
		return ObservationResult(
			tool="learn_business_logic",
			success=True,
			output=f"Loaded cached spec: {len(spec.input_sheets)} input sheets, "
			f"{len(spec.output_sheets)} output sheets, "
			f"{len(spec.transformation_rules)} rules, "
			f"{len(spec.semantic_validations)} validations",
			data=spec,
		)

	lm = get_lm()
	source_code = get_source_code()
	working_sample = get_file_sample(working_input_path)
	output_sample = get_file_sample(output_path)

	with dspy.context(lm=lm):
		# Step 1: Understand input
		logger.info("Learn Step 1/3: Understanding input schema...")
		input_result = dspy.ChainOfThought(InputSchemaSignature)(
			working_input_sample=working_sample,
			source_code=source_code,
		)
		input_sheets = input_result.input_sheets

		# Step 2: Understand output
		logger.info("Learn Step 2/3: Understanding output schema...")
		output_result = dspy.ChainOfThought(OutputSchemaSignature)(
			output_sample=output_sample,
		)
		output_sheets = output_result.output_sheets

		# Step 3: Extract transformation rules
		logger.info("Learn Step 3/3: Extracting transformation rules...")
		input_schema_json = InputSheetSignature.model_json_schema()
		transform_result = dspy.ChainOfThought(TransformationSignature)(
			input_schema=str([s.model_dump() for s in input_sheets]),
			output_schema=str([s.model_dump() for s in output_sheets]),
			source_code=source_code,
		)

	spec = BusinessLogicSpec(
		input_sheets=input_sheets,
		output_sheets=output_sheets,
		transformation_rules=transform_result.transformation_rules,
		semantic_validations=transform_result.semantic_validations,
		output_file_format="xlsx + csv",
	)

	cache_path.write_text(spec.model_dump_json(indent=2))
	logger.info("BusinessLogicSpec cached to %s", cache_path)

	return ObservationResult(
		tool="learn_business_logic",
		success=True,
		output=f"Extracted spec: {len(spec.input_sheets)} input sheets, "
		f"{len(spec.output_sheets)} output sheets, "
		f"{len(spec.transformation_rules)} rules, "
		f"{len(spec.semantic_validations)} validations",
		data=spec,
	)


def tool_identify_sheets(
	spec: BusinessLogicSpec | None = None,
	new_file_sample: str = "",
) -> ObservationResult:
	"""
	identify_sheets() -> str
	Identify which sheets in the new file match expected semantic roles.
	Returns:
	    Summary of sheet matches with confidence scores.
	"""
	if spec is None:
		return ObservationResult(
			tool="identify_sheets",
			success=False,
			output="BusinessLogicSpec not provided. Run learn_business_logic first.",
		)

	lm = get_lm()
	with dspy.context(lm=lm):
		result = dspy.ChainOfThought(SheetIdentificationSignature)(
			input_sheet_signatures=str([s.model_dump() for s in spec.input_sheets]),
			new_file_sample=new_file_sample,
		)

	matches = result.sheet_matches
	summary_lines = []
	for m in matches:
		status = "HIGH" if m.confidence >= 0.70 else "MEDIUM" if m.confidence >= 0.50 else "LOW"
		summary_lines.append(
			f"  {m.semantic_role} -> '{m.actual_sheet_name}' "
			f"(confidence={m.confidence:.2f}, {status}, critical={m.critical})"
		)

	return ObservationResult(
		tool="identify_sheets",
		success=True,
		output="Sheet matches:\n" + "\n".join(summary_lines),
		data=matches,
	)


def tool_map_columns(
	spec: BusinessLogicSpec | None = None,
	sheet_matches: list[SheetMatch] | None = None,
	new_file_path: str = "",
) -> ObservationResult:
	"""
	map_columns() -> str
	Map columns in each identified sheet to expected semantic columns.
	Returns:
	    Summary of column matches with confidence scores.
	"""
	if spec is None or sheet_matches is None:
		return ObservationResult(
			tool="map_columns",
			success=False,
			output="Spec or sheet_matches not provided.",
		)

	lm = get_lm()
	xl = pd.ExcelFile(new_file_path)
	all_column_matches: list[ColumnMatch] = []

	with dspy.context(lm=lm):
		for sheet_match in sheet_matches:
			if not sheet_match.actual_sheet_name or sheet_match.confidence < 0.50:
				continue

			# Find the spec for this role
			sheet_spec = next(
				(s for s in spec.input_sheets if s.semantic_role == sheet_match.semantic_role),
				None,
			)
			if not sheet_spec:
				continue

			# Get sample from the actual sheet
			df = pd.read_excel(xl, sheet_name=sheet_match.actual_sheet_name)
			sample = tabulate(df.head(5), headers="keys", tablefmt="pipe", showindex=False)

			logger.info(
				"Mapping columns for '%s' (role: %s)...",
				sheet_match.actual_sheet_name,
				sheet_match.semantic_role,
			)

			result = dspy.ChainOfThought(ColumnMappingSignature)(
				expected_columns=str([c.model_dump() for c in sheet_spec.columns]),
				actual_sheet_sample=f"Sheet: {sheet_match.actual_sheet_name}\n{sample}",
			)

			# Tag each match with the sheet role
			for cm in result.column_matches:
				cm.sheet_role = sheet_match.semantic_role
			all_column_matches.extend(result.column_matches)

	# Build InputMapping
	missing = [
		cm.semantic_name
		for cm in all_column_matches
		if cm.actual_column_name is None and cm.critical
	]

	# Determine feasibility
	critical_low = any(sm.confidence < 0.50 and sm.critical for sm in sheet_matches) or any(
		cm.confidence < 0.50 and cm.critical for cm in all_column_matches
	)
	any_medium = any((0.50 <= sm.confidence < 0.70) and sm.critical for sm in sheet_matches) or any(
		(0.50 <= cm.confidence < 0.70) and cm.critical for cm in all_column_matches
	)

	if critical_low:
		feasibility = "terminate"
	elif any_medium:
		feasibility = "ask_human"
	else:
		feasibility = "proceed"

	# Overall confidence
	all_confidences = [sm.confidence for sm in sheet_matches]
	all_confidences.extend(cm.confidence for cm in all_column_matches)
	overall = sum(all_confidences) / max(len(all_confidences), 1)

	mapping = InputMapping(
		sheet_matches=sheet_matches,
		column_matches=all_column_matches,
		missing_fields=missing,
		feasibility=feasibility,
		overall_confidence=overall,
	)

	summary_lines = [f"Overall confidence: {overall:.2f}, feasibility: {feasibility}"]
	if missing:
		summary_lines.append(f"Missing critical fields: {', '.join(missing)}")
	for cm in all_column_matches:
		if cm.actual_column_name != cm.semantic_name:
			summary_lines.append(
				f"  {cm.sheet_role}.{cm.semantic_name} -> '{cm.actual_column_name}' "
				f"(confidence={cm.confidence:.2f})"
			)

	return ObservationResult(
		tool="map_columns",
		success=True,
		output="Column mapping:\n" + "\n".join(summary_lines),
		data=mapping,
	)


def tool_generate_code(
	spec: BusinessLogicSpec | None = None,
	mapping: InputMapping | None = None,
	new_file_sample: str = "",
	user_instructions: str = "none",
) -> ObservationResult:
	"""
	generate_code() -> str
	Generate a Python transformation script.
	Returns:
	    Summary and the generated script.
	"""
	if spec is None or mapping is None:
		return ObservationResult(
			tool="generate_code",
			success=False,
			output="Spec or mapping not provided.",
		)

	lm = get_lm()
	reference_code = get_source_code()

	with dspy.context(lm=lm):
		result = dspy.ChainOfThought(CodeGenerationSignature)(
			business_logic_spec=spec.model_dump_json(),
			input_mapping=mapping.model_dump_json(),
			new_file_sample=new_file_sample,
			reference_code=reference_code,
			user_instructions=user_instructions,
		)

	script = result.python_script

	# Clean up markdown code fences if present
	if script.startswith("```"):
		lines = script.split("\n")
		lines = lines[1:]  # remove opening ```python
		if lines and lines[-1].strip() == "```":
			lines = lines[:-1]
		script = "\n".join(lines)

	# Save to generated_scripts/
	scripts_dir = Path("./generated_scripts")
	scripts_dir.mkdir(exist_ok=True)
	script_path = scripts_dir / "transform.py"
	script_path.write_text(script)
	logger.info("Generated script saved to %s", script_path)

	return ObservationResult(
		tool="generate_code",
		success=True,
		output=f"Script generated and saved to {script_path} ({len(script.splitlines())} lines)",
		data=str(script_path),
	)


def tool_execute_code(
	script_path: str = "",
	input_file: str = "",
	output_dir: str = "./outputs",
) -> ObservationResult:
	"""
	execute_code() -> str
	Run the generated script in a subprocess.
	Returns:
	    stdout + stderr from the script.
	"""
	cmd = [sys.executable, script_path, input_file, output_dir]
	output_path = Path(output_dir)
	output_path.mkdir(exist_ok=True)

	def _signature_map() -> dict[Path, tuple[int, int]]:
		return {
			path: (path.stat().st_mtime_ns, path.stat().st_size)
			for path in output_path.iterdir()
			if path.is_file()
		}

	try:
		before = _signature_map()
		result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
		success = result.returncode == 0
		output = result.stdout + result.stderr

		output_files: list[str] = []
		if success:
			after = _signature_map()
			output_files = sorted(
				str(path)
				for path, signature in after.items()
				if before.get(path) != signature
			)

		if output_files:
			file_list = "\n".join(output_files)
			output += f"\n\nOutput files:\n{file_list}"
			logger.info("Output files created", files=output_files)

		return ObservationResult(
			tool="execute_code", success=success, output=output, data=output_files
		)
	except subprocess.TimeoutExpired:
		return ObservationResult(
			tool="execute_code",
			success=False,
			output="Script timed out after 120s",
			data=[],
		)
	except Exception as e:
		return ObservationResult(tool="execute_code", success=False, output=str(e), data=[])


def tool_validate_output(
	spec: BusinessLogicSpec | None = None,
	mapping: InputMapping | None = None,
	output_dir: str = "./outputs",
	input_file: str = "",
) -> ObservationResult:
	"""
	validate_output() -> str
	Validate the generated output against the expected format.
	Returns:
	    Validation results — pass/fail with details.
	"""
	if spec is None:
		return ObservationResult(tool="validate_output", success=False, output="Spec not provided.")

	output_path = Path(output_dir)
	errors: list[str] = []

	# Find the audit xlsx file
	xlsx_files = list(output_path.glob("Vendor Payment Processing Audit*.xlsx"))
	if not xlsx_files:
		return ObservationResult(
			tool="validate_output",
			success=False,
			output="No audit xlsx file found in output directory.",
		)

	audit_file = xlsx_files[-1]  # most recent
	xl = pd.ExcelFile(audit_file)

	# Structural validation: check expected sheets exist
	for sheet_spec in spec.output_sheets:
		if sheet_spec.name not in xl.sheet_names:
			errors.append(f"Missing output sheet: '{sheet_spec.name}'")
			continue

		df = pd.read_excel(xl, sheet_name=sheet_spec.name)

		# Check expected columns
		for col_spec in sheet_spec.columns:
			if col_spec.name not in df.columns:
				errors.append(f"Missing column '{col_spec.name}' in sheet '{sheet_spec.name}'")

	# Row count sanity: read input to compare
	if input_file:
		input_xl = pd.ExcelFile(input_file)
		total_input_rows = sum(
			len(pd.read_excel(input_xl, sheet_name=s)) for s in input_xl.sheet_names
		)

	# Semantic validations
	for validation in spec.semantic_validations:
		logger.info("Checking semantic validation: %s", validation.name)
		# These are checked based on the logic description
		# For now, we do concrete checks for known patterns
		try:
			if "hold" in validation.name.lower() or "hold" in validation.check_logic.lower():
				# Check no hold vendors in payments
				if "Vendor Payments" in xl.sheet_names and mapping:
					vp = pd.read_excel(xl, sheet_name="Vendor Payments")
					# Find the hold list sheet in the input
					hold_match = next(
						(
							sm
							for sm in mapping.sheet_matches
							if "hold" in sm.semantic_role.lower()
							or "block" in sm.semantic_role.lower()
						),
						None,
					)
					if hold_match and hold_match.actual_sheet_name and input_file:
						hold_df = pd.read_excel(input_file, sheet_name=hold_match.actual_sheet_name)
						if len(hold_df.columns) > 0:
							hold_vendors = set(hold_df.iloc[:, 0].dropna().astype(str).str.lower())
							if "Vendor Name" in vp.columns:
								payment_vendors = set(
									vp["Vendor Name"].dropna().astype(str).str.lower()
								)
								overlap = hold_vendors & payment_vendors
								if overlap:
									errors.append(
										f"Semantic validation '{validation.name}' FAILED: "
										f"hold vendors found in payments: {overlap}"
									)

			if "sequential" in validation.check_logic.lower():
				if "Vendor Payments" in xl.sheet_names:
					vp = pd.read_excel(xl, sheet_name="Vendor Payments")
					if "Bill Payment ID" in vp.columns:
						ids = vp["Bill Payment ID"].tolist()
						expected = list(range(1, len(ids) + 1))
						if ids != expected:
							errors.append(
								f"Semantic validation '{validation.name}' FAILED: "
								f"Bill Payment IDs are not sequential"
							)

			if "duplicate" in validation.check_logic.lower():
				if "Vendor Payments" in xl.sheet_names:
					vp = pd.read_excel(xl, sheet_name="Vendor Payments")
					if "Invoice Number" in vp.columns:
						dupes = vp["Invoice Number"].duplicated().sum()
						if dupes > 0:
							errors.append(
								f"Semantic validation '{validation.name}' FAILED: "
								f"{dupes} duplicate invoice numbers found"
							)
		except Exception as e:
			logger.warning("Semantic validation '%s' skipped: %s", validation.name, e)

	if errors:
		return ObservationResult(
			tool="validate_output",
			success=False,
			output="Validation FAILED:\n" + "\n".join(f"  - {e}" for e in errors),
		)

	return ObservationResult(
		tool="validate_output",
		success=True,
		output=f"Validation PASSED: {len(xl.sheet_names)} sheets, all checks passed.",
	)


def tool_fix_code(
	script_path: str = "",
	error_message: str = "",
	spec: BusinessLogicSpec | None = None,
	mapping: InputMapping | None = None,
) -> ObservationResult:
	"""
	fix_code() -> str
	Fix a generated script that failed during execution.
	Returns:
	    Summary of fix.
	"""
	if spec is None:
		return ObservationResult(tool="fix_code", success=False, output="Spec not provided.")

	script = Path(script_path).read_text()
	lm = get_lm()

	mapping_json = mapping.model_dump_json() if mapping else "{}"

	with dspy.context(lm=lm):
		result = dspy.ChainOfThought(CodeFixSignature)(
			python_script=script,
			error_message=error_message,
			business_logic_spec=spec.model_dump_json(),
			input_mapping=mapping_json,
		)

	fixed = result.fixed_python_script
	if fixed.startswith("```"):
		lines = fixed.split("\n")
		lines = lines[1:]
		if lines and lines[-1].strip() == "```":
			lines = lines[:-1]
		fixed = "\n".join(lines)

	Path(script_path).write_text(fixed)
	logger.info("Fixed script saved to %s", script_path)

	return ObservationResult(
		tool="fix_code",
		success=True,
		output=f"Script fixed and saved to {script_path}",
		data=script_path,
	)


def tool_ask_human(question: str) -> ObservationResult:
	"""
	ask_human(question: str) -> str
	Ask the user a question.
	Args:
	    question: the question to display
	Returns:
	    The user's response.
	"""
	print(f"\n{'=' * 50}")
	print("[HUMAN INPUT REQUIRED]")
	print(question)
	print(f"{'=' * 50}")
	response = input("Your answer: ").strip()
	return ObservationResult(tool="ask_human", success=True, output=response)


def tool_finish(summary: str) -> ObservationResult:
	"""
	finish(summary: str) -> str
	Signal successful completion.
	Args:
	    summary: what was accomplished
	Returns:
	    The summary.
	"""
	return ObservationResult(tool="finish", success=True, output=summary)


def tool_terminate(reason: str) -> ObservationResult:
	"""
	terminate(reason: str) -> str
	Stop processing — file cannot be handled.
	Args:
	    reason: why the file was rejected
	Returns:
	    The reason.
	"""
	return ObservationResult(tool="terminate", success=False, output=reason)


# ── Tool Registry ────────────────────────────────────────────────────────────

TOOLS = {
	"learn_business_logic": tool_learn_business_logic,
	"identify_sheets": tool_identify_sheets,
	"map_columns": tool_map_columns,
	"generate_code": tool_generate_code,
	"execute_code": tool_execute_code,
	"validate_output": tool_validate_output,
	"fix_code": tool_fix_code,
	"ask_human": tool_ask_human,
	"finish": tool_finish,
	"terminate": tool_terminate,
}

ANTHROPIC_TOOLS = [
	{
		"name": "learn_business_logic",
		"description": "Extract BusinessLogicSpec from working input, process.py, and output. Run once.",
		"input_schema": {"type": "object", "properties": {}, "required": []},
	},
	{
		"name": "identify_sheets",
		"description": "Identify which sheets in the new file match expected semantic roles. Run after learn_business_logic.",
		"input_schema": {"type": "object", "properties": {}, "required": []},
	},
	{
		"name": "map_columns",
		"description": "Map columns in each identified sheet to expected semantic columns. Run after identify_sheets.",
		"input_schema": {"type": "object", "properties": {}, "required": []},
	},
	{
		"name": "generate_code",
		"description": "Generate a Python transformation script. Run after map_columns.",
		"input_schema": {
			"type": "object",
			"properties": {
				"user_instructions": {
					"type": "string",
					"description": "Additional instructions from the user, or 'none'",
				},
			},
			"required": [],
		},
	},
	{
		"name": "execute_code",
		"description": "Run the generated transformation script.",
		"input_schema": {"type": "object", "properties": {}, "required": []},
	},
	{
		"name": "validate_output",
		"description": "Validate the generated output against expected format (structural + semantic).",
		"input_schema": {"type": "object", "properties": {}, "required": []},
	},
	{
		"name": "fix_code",
		"description": "Fix a generated script that failed. Provide the error message.",
		"input_schema": {
			"type": "object",
			"properties": {
				"error_message": {
					"type": "string",
					"description": "Error output from the failed execution",
				},
			},
			"required": ["error_message"],
		},
	},
	{
		"name": "ask_human",
		"description": "Ask the user a question for clarification or additional context.",
		"input_schema": {
			"type": "object",
			"properties": {
				"question": {
					"type": "string",
					"description": "The question to ask",
				},
			},
			"required": ["question"],
		},
	},
	{
		"name": "finish",
		"description": "Signal successful completion.",
		"input_schema": {
			"type": "object",
			"properties": {
				"summary": {
					"type": "string",
					"description": "What was accomplished",
				},
			},
			"required": ["summary"],
		},
	},
	{
		"name": "terminate",
		"description": "Stop processing — file cannot be handled.",
		"input_schema": {
			"type": "object",
			"properties": {
				"reason": {
					"type": "string",
					"description": "Why the file was rejected",
				},
			},
			"required": ["reason"],
		},
	},
]
