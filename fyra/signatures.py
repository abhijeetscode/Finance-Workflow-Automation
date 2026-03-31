"""DSPy Signatures — no manual prompts anywhere in this file."""

from pydantic import BaseModel
import dspy


# ---------------------------------------------------------------------------
# Shared data models
# ---------------------------------------------------------------------------

class SOPStep(BaseModel):
    step_number: int
    description: str
    input_files: list[str]
    expected_output: str


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------

class ParseSOPToSteps(dspy.Signature):
    """Parse a financial SOP document into an ordered list of executable steps.
    Use file_context to understand what each file represents so steps are grounded."""

    sop_text: str = dspy.InputField()
    file_context: str = dspy.InputField(
        desc="JSON dict mapping filename to what it represents in this workflow"
    )
    steps: list[SOPStep] = dspy.OutputField(
        desc="Ordered list of steps; each step references only files present in file_context"
    )


class ExecuteSOPStep(dspy.Signature):
    """Execute one step of a financial SOP using the available tools.
    Read input files first, then transform and write data to the output Excel file.
    Prefer bulk range operations over row-by-row loops.
    For formula copies, always build substitutions for the period change before copying."""

    step_description: str = dspy.InputField()
    file_context: str = dspy.InputField(
        desc="JSON dict mapping filename to its purpose"
    )
    output_file_path: str = dspy.InputField(
        desc="Working copy of the output Excel file — all writes go here"
    )
    input_file_paths: str = dspy.InputField(
        desc="JSON list of input file paths available to read"
    )
    result: str = dspy.OutputField(
        desc="Plain-English summary of what was done: which sheets were written, how many rows, which formulas were copied"
    )


class BuildFormulaSubstitutions(dspy.Signature):
    """Build a substitution dict covering every date/period format variant found in Excel formulas.
    e.g. source=January 2026, target=February 2026 →
    {'Jan-26': 'Feb-26', '0126': '0226', \"Jan'26\": \"Feb'26\", 'January 2026': 'February 2026'}"""

    source_period: str = dspy.InputField(desc="Human-readable source period, e.g. 'January 2026'")
    target_period: str = dspy.InputField(desc="Human-readable target period, e.g. 'February 2026'")
    substitutions: dict[str, str] = dspy.OutputField(
        desc="All format variants that may appear in formula strings or sheet names"
    )
