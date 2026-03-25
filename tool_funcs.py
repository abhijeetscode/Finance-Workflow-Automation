import os
import subprocess
import sys
from collections.abc import Callable
from typing import Any

import dspy
import pandas as pd
from dotenv import load_dotenv
from pydantic import BaseModel, model_validator

load_dotenv()


# ── Models ────────────────────────────────────────────────────────────────────


class ObservationResult(BaseModel):
	tool: str
	success: bool
	output: str
	data: Any = None


class SheetMapping(BaseModel):
	expected: str
	actual: str | None
	confidence: float
	critical: bool


class ColumnMapping(BaseModel):
	expected_column: str
	actual_column: str | None
	sheet_expected: str
	sheet_actual: str | None
	confidence: float
	critical: bool


class DriftResolution(BaseModel):
	sheet_mappings: list[SheetMapping]
	column_mappings: list[ColumnMapping]
	domain_match: bool
	reasoning: str

	@model_validator(mode="before")
	@classmethod
	def normalize_column_mappings(cls, values: dict[str, object]) -> dict[str, object]:
		"""LLM returns column_mappings as nested dict — flatten to list."""
		cm = values.get("column_mappings")
		if not isinstance(cm, dict):
			return values

		flat: list[dict[str, object]] = []
		for sheet_name, columns in cm.items():
			if not isinstance(columns, dict):
				continue
			for col_name, mapping in columns.items():
				if not isinstance(mapping, dict):
					continue
				flat.append(
					{
						"expected_column": mapping.get("expected", col_name),
						"actual_column": mapping.get("actual", col_name),
						"sheet_expected": sheet_name,
						"sheet_actual": sheet_name,
						"confidence": mapping.get("confidence", 0.0),
						"critical": mapping.get("critical", False),
					}
				)
		values["column_mappings"] = flat
		return values


# ── Drift Resolution Signature ────────────────────────────────────────────────


class DriftResolutionSignature(dspy.Signature):
	"""
	Given a golden schema (extracted from a known-working file and execution agent)
	and a sample from a new input file, identify how the new file maps to the
	expected schema. For each expected sheet and column, find the best match in
	the new file and assign a confidence score between 0.0 and 1.0.
	Also determine if the new file is about the same domain as the execution agent.
	"""

	golden_schema: str = dspy.InputField(
		desc="Golden schema as JSON string — canonical sheet and column names with their purpose"
	)
	new_file_sample: str = dspy.InputField(
		desc="Tabular sample from the new input file showing sheet names and row data"
	)
	drift_resolution: DriftResolution = dspy.OutputField(
		desc=(
			"Mapping of expected sheets and columns to actual ones found in the new file. "
			"sheet_mappings must be a flat list of objects with keys: expected, actual, confidence, critical. "
			"column_mappings must be a flat list of objects with keys: expected_column, actual_column, sheet_expected, sheet_actual, confidence, critical. "
			"Do NOT nest column_mappings by sheet name — it must always be a flat list. "
			"confidence 1.0 = exact match, 0.0 = not found. actual is None if no match found."
		)
	)


# ── Tools ─────────────────────────────────────────────────────────────────────

AGENT_SCRIPT_PATH = "./code/process.py"


def tool_execute_agent(input_file: str, date: str | None = None) -> ObservationResult:
	"""
	execute_agent(input_file: str, date: str | None) -> str
	Run the execution agent on the input file.
	Args:
	    input_file: path to the input Excel file
	    date: optional processing date in YYYY-MM-DD format, or None
	Returns:
	    stdout + stderr from the execution agent process.
	"""
	cmd = [sys.executable, AGENT_SCRIPT_PATH, input_file]
	if date:
		cmd += ["--date", date]

	try:
		result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
		success = result.returncode == 0
		output = result.stdout + result.stderr
		return ObservationResult(tool="execute_agent", success=success, output=output)
	except subprocess.TimeoutExpired:
		return ObservationResult(
			tool="execute_agent", success=False, output="Execution timed out after 120s"
		)
	except Exception as e:
		return ObservationResult(tool="execute_agent", success=False, output=str(e))


def tool_ask_human(question: str) -> ObservationResult:
	"""
	ask_human(question: str) -> str
	Pause and ask the human a clarification or confirmation question.
	Args:
	    question: the question to display to the human
	Returns:
	    The human's response as a string.
	"""
	print(f"\n{'=' * 50}")
	print("[HUMAN INPUT REQUIRED]")
	print(question)
	print(f"{'=' * 50}")
	response = input("Your answer: ").strip()
	return ObservationResult(tool="ask_human", success=True, output=response)


def tool_resolve_schema(golden_schema: str, new_file_sample: str) -> ObservationResult:
	"""
	resolve_schema(golden_schema: str, new_file_sample: str) -> str
	Compare the new file against the golden schema and return confidence-scored mappings.
	Args:
	    golden_schema: golden schema JSON string
	    new_file_sample: tabular string sample from the new input file
	Returns:
	    JSON string with sheet_mappings, column_mappings, domain_match, reasoning.
	    Each mapping has: expected, actual, confidence (0.0-1.0), critical.
	"""
	lm = dspy.LM(
		"gpt-4o-mini",
		temperature=0.0,
		api_key=os.getenv("OPENAI_API_KEY"),
	)
	predictor = dspy.ChainOfThought(DriftResolutionSignature)

	with dspy.context(lm=lm):
		result = predictor(
			golden_schema=golden_schema,
			new_file_sample=new_file_sample,
		)

	resolution = result.drift_resolution
	avg_confidence = sum(m.confidence for m in resolution.sheet_mappings) / max(
		len(resolution.sheet_mappings), 1
	)
	return ObservationResult(
		tool="resolve_schema",
		success=True,
		output=(
			f"domain_match={resolution.domain_match}, "
			f"sheets={len(resolution.sheet_mappings)}, "
			f"columns={len(resolution.column_mappings)}, "
			f"avg_confidence={avg_confidence:.2f}, "
			f"reasoning={resolution.reasoning}"
		),
		data=resolution,
	)


def tool_normalize_file(
	input_file: str, drift_resolution: DriftResolution | None = None
) -> ObservationResult:
	"""
	normalize_file(input_file: str) -> str
	Rename sheets and columns in the input file based on the resolved schema mappings.
	Call this after resolve_schema succeeds and before execute_agent.
	Args:
	    input_file: path to the drifted input Excel file
	Returns:
	    Path to the normalized output Excel file ready for the execution agent.
	"""
	from pathlib import Path

	if drift_resolution is None:
		return ObservationResult(
			tool="normalize_file",
			success=False,
			output="resolve_schema must be called before normalize_file",
		)

	resolution = drift_resolution
	xl = pd.ExcelFile(input_file)
	renamed_sheets: list[str] = []
	renamed_columns: list[str] = []

	# strip sheet and column names from drift resolution
	for m in resolution.sheet_mappings:
		m.expected = m.expected.strip()
		if m.actual:
			m.actual = m.actual.strip()
	for m in resolution.column_mappings:
		m.expected_column = m.expected_column.strip()
		m.sheet_expected = m.sheet_expected.strip()
		if m.actual_column:
			m.actual_column = m.actual_column.strip()
		if m.sheet_actual:
			m.sheet_actual = m.sheet_actual.strip()

	# build sheet rename map: actual -> expected
	sheet_rename = {
		m.actual: m.expected
		for m in resolution.sheet_mappings
		if m.actual and m.actual != m.expected and m.actual in xl.sheet_names
	}

	# build column rename map per expected sheet name
	col_rename: dict[str, dict[str, str]] = {}
	for m in resolution.column_mappings:
		if m.actual_column and m.actual_column != m.expected_column:
			expected_sheet = m.sheet_expected
			col_rename.setdefault(expected_sheet, {})[m.actual_column] = m.expected_column

	output_path = Path(input_file).with_stem(Path(input_file).stem + "_normalized")
	with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
		for sheet_name in xl.sheet_names:
			df = pd.read_excel(xl, sheet_name=sheet_name)
			# strip column names from the actual file
			df.columns = df.columns.str.strip()

			# determine the expected sheet name after rename
			expected_name = sheet_rename.get(sheet_name, sheet_name)
			if sheet_name != expected_name:
				renamed_sheets.append(f"'{sheet_name}' -> '{expected_name}'")

			# rename columns if needed
			if expected_name in col_rename:
				for old, new in col_rename[expected_name].items():
					if old in df.columns:
						renamed_columns.append(f"'{old}' -> '{new}' on '{expected_name}'")
				df = df.rename(columns=col_rename[expected_name])

			df.to_excel(writer, sheet_name=expected_name, index=False)

	summary = f"Saved to {output_path}"
	if renamed_sheets:
		summary += f"\nRenamed sheets: {', '.join(renamed_sheets)}"
	if renamed_columns:
		summary += f"\nRenamed columns: {', '.join(renamed_columns)}"

	return ObservationResult(
		tool="normalize_file",
		success=True,
		output=summary,
		data=str(output_path),
	)


def tool_finish(summary: str) -> ObservationResult:
	"""
	finish(summary: str) -> str
	Signal that the task completed successfully.
	Args:
	    summary: brief summary of what was done and the result
	Returns:
	    The summary string.
	"""
	return ObservationResult(tool="finish", success=True, output=summary)


def tool_terminate(reason: str) -> ObservationResult:
	"""
	terminate(reason: str) -> str
	Stop processing because the input file cannot be handled.
	Args:
	    reason: explanation of why the file was rejected (e.g. wrong domain, missing critical data)
	Returns:
	    The reason string.
	"""
	return ObservationResult(tool="terminate", success=False, output=reason)


TOOLS: dict[str, Callable[..., ObservationResult]] = {
	"resolve_schema": tool_resolve_schema,
	"normalize_file": tool_normalize_file,
	"execute_agent": tool_execute_agent,
	"ask_human": tool_ask_human,
	"finish": tool_finish,
	"terminate": tool_terminate,
}


OPENAI_TOOLS = [
	{
		"type": "function",
		"function": {
			"name": "resolve_schema",
			"description": "Compare the new file against the golden schema and return confidence-scored mappings.",
			"parameters": {
				"type": "object",
				"properties": {
					"golden_schema": {
						"type": "string",
						"description": "Golden schema as JSON string",
					},
					"new_file_sample": {
						"type": "string",
						"description": "Tabular string sample from the new input file",
					},
				},
				"required": ["golden_schema", "new_file_sample"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "normalize_file",
			"description": "Rename sheets and columns in the input file using the drift resolution from resolve_schema. Call after resolve_schema and before execute_agent.",
			"parameters": {
				"type": "object",
				"properties": {},
				"required": [],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "execute_agent",
			"description": "Run the execution agent on the normalized file. Must call normalize_file first.",
			"parameters": {
				"type": "object",
				"properties": {
					"date": {
						"type": "string",
						"description": "Optional processing date in YYYY-MM-DD format",
					},
				},
				"required": [],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "ask_human",
			"description": "Pause and ask the human a clarification or confirmation question. Use when confidence is between 0.50-0.70.",
			"parameters": {
				"type": "object",
				"properties": {
					"question": {
						"type": "string",
						"description": "The question to display to the human",
					},
				},
				"required": ["question"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "finish",
			"description": "Signal that the task completed successfully. Call after execute_agent succeeds.",
			"parameters": {
				"type": "object",
				"properties": {
					"summary": {
						"type": "string",
						"description": "Brief summary of what was done and the result",
					},
				},
				"required": ["summary"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "terminate",
			"description": "Stop processing because the input file cannot be handled. Call when domain_match is false, critical fields are missing, or confidence is too low.",
			"parameters": {
				"type": "object",
				"properties": {
					"reason": {
						"type": "string",
						"description": "Explanation of why the file was rejected",
					},
				},
				"required": ["reason"],
			},
		},
	},
]
