"""
SOP Agent — orchestrates step-by-step execution with human verification.
"""

import json
import logging
from pathlib import Path

import dspy

from signatures import BuildFormulaSubstitutions, ExecuteSOPStep, SOPStep
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
        """Read all data from an Excel sheet (.xls or .xlsx). Returns JSON array of rows."""
        return read_excel(path, sheet)

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
            path, src_sheet, src_range, dst_sheet, dst_start_cell,
            substitutions_json, col_offset, row_offset, step_number_ref[0],
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

    return [
        read_excel_tool,
        list_sheets_tool,
        append_to_sheet_tool,
        create_sheet_tool,
        copy_formula_tool,
        build_formula_substitutions_tool,
        read_xml_tool,
        extract_html_tables_tool,
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
    lm = dspy.LM("openai/gpt-4o-mini", max_tokens=4096)
    # lm = dspy.LM("anthropic/claude-sonnet-4-6", max_tokens=4096)
    dspy.configure(lm=lm)

    # Step 1 — gather file context from human
    file_context = gather_file_context(input_files)
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
                description=step.description + f"\n\n[Retry] Human feedback from previous attempt: {feedback}",
                input_files=step.input_files,
                expected_output=step.expected_output,
            )

    logger.info("All steps complete. Output: %s", output_path)
    print(f"\nDone. Output file: {output_path}")
