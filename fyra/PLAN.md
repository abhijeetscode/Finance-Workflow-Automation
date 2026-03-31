# SOP Automation Agent — Implementation Plan

## Overview

A POC agent that accepts any financial SOP (docx) + input files, parses the SOP into ordered steps using DSPy, and executes each step sequentially with human verification before and after each write operation.

---

## Folder Structure

```
fyra/
  main.py              # entry point (argparse)
  agent.py             # orchestration loop
  sop_parser.py        # DSPy: SOP text → structured steps
  signatures.py        # all DSPy Signatures (no manual prompts)
  tools.py             # all tool implementations
  human_loop.py        # all human-in-the-loop interactions
  pyproject.toml
  PLAN.md
```

---

## `signatures.py`

Four DSPy signatures — no manual prompt strings anywhere.

```
ParseSOPToSteps
  input:  sop_text, file_context (dict: filename → what it represents)
  output: steps[] — each has: step_number, description, input_files[], expected_output

PlanStepExecution
  input:  step_description, file_context, available_tools[]
  output: execution_plan (ordered list of tool calls with arguments)

BuildFormulaSubstitutions
  input:  source_period, target_period
  output: substitutions dict covering all format variants
          e.g. {"Jan-26": "Feb-26", "0126": "0226", "Jan'26": "Feb'26"}

RetryWithFeedback
  input:  original_step, previous_execution_plan, human_feedback
  output: revised_execution_plan
```

---

## Core Design Rule — Bulk First

**Always prefer bulk/range operations over row-by-row or cell-by-cell loops.**

- Tools accept ranges (e.g. `A1:Z100`), full datasets, or entire sheet data — not single rows or cells
- Formula transformations are applied to an entire range in one pass — not formula by formula
- XML table extraction returns all tables at once — not one row at a time
- `append_to_sheet` writes the full dataset in a single `worksheet.append()` loop — no per-cell writes
- `copy_formula` processes the entire source range in one openpyxl read → transform → write pass
- DSPy plans bulk tool calls: e.g. "append all 4 asset type blocks in one call with `after_keyword`" rather than 4 separate append calls

This keeps runs fast, reduces human verification interruptions, and makes dry-run previews meaningful (you see the full batch, not one row).

---

## `tools.py`

All tools are pure functions. No LLM logic. All Excel operations use **openpyxl** only — no pandas.

### Excel Tools

```
read_excel(path, sheet)
  → reads entire sheet in one pass via openpyxl
  → returns all rows as list of dicts (bulk — caller filters/slices as needed)

list_sheets(path)
  → returns all sheet names[]

find_write_row(path, sheet, start_col, end_col, after_keyword=None)
  → scans rows in column range for first empty row
  → if after_keyword given: finds that header row first, then scans below it
  → handles sparse sheets with empty rows interspersed between data blocks
  → returns row number

append_to_sheet(path, sheet, data, start_row, start_col, dry_run=False)
  → data is the full dataset (list of rows) — written in a single bulk pass via openpyxl
  → path must point to the copy inside output/ — enforced at runtime (raises if path is outside output/)
  → raises if any target cell is non-empty (never overwrites)
  → if dry_run=True: returns row count, column range, and first 5 rows as preview

create_sheet(path, sheet_name)
  → adds new sheet to existing workbook via openpyxl

copy_formula(src_path, src_sheet, src_range,
             dst_path, dst_sheet, dst_cell,
             substitutions=None, col_offset=0, row_offset=0,
             dry_run=False)
  → reads entire src_range in one openpyxl pass (data_only=False)
  → applies all substitutions and offsets across the full range in one transform pass
  → writes entire transformed range to dst in one openpyxl write pass
  → if dry_run=True: returns full list of (cell_ref, original_formula, transformed_formula)
  → if dry_run=False: writes to dst workbook
```

### XML Tools

```
read_xml(path)
  → lxml etree.parse → returns structured dict

extract_html_tables_from_xml(path)
  → lxml parses outer XML structure
  → BeautifulSoup4 finds and extracts all <table> tags in one pass
  → returns all tables at once as list-of-dicts (column headers as keys)
  → caller selects which table(s) to use — no partial extraction
```

### Human Tool

```
ask_human(question, context=None)
  → prints question + optional context to terminal
  → blocks waiting for string input
  → returns response string
```

### Dry-run Wrapper

```
preview_action(tool_name, args)
  → calls the tool with dry_run=True
  → returns human-readable summary of what WOULD happen
  → no files are touched
```

---

## `human_loop.py`

Two interaction points, both backed by `ask_human`.

### 1. File Context Gathering (upfront, before SOP parsing)

```python
def gather_file_context(file_paths):
    context = {}
    for path in file_paths:
        answer = ask_human(
            question=f"What does '{filename}' represent in this workflow?",
            context="This helps the agent correctly interpret the SOP steps."
        )
        context[filename] = answer
    return context
    # example output:
    # {
    #   "FAM_SummaryReport_FPR72456.xml": "Asset Summary Report for Feb 2026",
    #   "EO - Fixed Assets Rollforward 2026_Feb26_YS.xlsx": "Target output workbook"
    # }
```

### 2. Step Verification (two checkpoints per step)

```python
def verify_step(step_number, preview_summary):
    # Checkpoint 1 — before any write
    answer = ask_human(
        question=f"[Step {step_number}] I am about to do the following. Proceed?",
        context=preview_summary    # what tool will do, which rows, which sheet, which range
    )
    if answer.lower() == "no":
        feedback = ask_human("What should be corrected?")
        return "retry", feedback

    # --- actual write happens here in agent.py ---

    # Checkpoint 2 — after write
    answer = ask_human(
        question=f"[Step {step_number}] Done. Does the output look correct?",
        context=result_summary     # what was actually written
    )
    if answer.lower() == "no":
        feedback = ask_human("What was wrong?")
        return "retry", feedback

    return "ok", None
```

---

## `sop_parser.py`

```python
class SOPParser(dspy.Module):
    def __init__(self):
        self.parse = dspy.ChainOfThought(ParseSOPToSteps)

    def forward(self, sop_text, file_context):
        return self.parse(sop_text=sop_text, file_context=file_context)
        # returns: steps[] in order, each step grounded in file_context
```

---

## `agent.py` — Core Orchestration

```python
class StepExecutor(dspy.Module):
    def __init__(self, tools):
        self.plan   = dspy.ChainOfThought(PlanStepExecution)
        self.retry  = dspy.ChainOfThought(RetryWithFeedback)
        self.f_subs = dspy.ChainOfThought(BuildFormulaSubstitutions)
        self.tools  = tools

    def execute_step(self, step, file_context, feedback=None, prev_plan=None):
        # re-plan if human gave feedback on previous attempt
        if feedback:
            plan = self.retry(
                original_step=step,
                previous_execution_plan=prev_plan,
                human_feedback=feedback
            )
        else:
            plan = self.plan(
                step_description=step.description,
                file_context=file_context,
                available_tools=list(self.tools.keys())
            )

        for tool_call in plan.execution_plan:
            # dry-run first
            preview = preview_action(tool_call)
            status, feedback = verify_step(step.step_number, preview)

            if status == "retry":
                return self.execute_step(step, file_context, feedback, plan)

            # human approved — execute for real
            run_actual(tool_call)

        return plan


def run_agent(sop_path, file_paths):
    file_context = gather_file_context(file_paths)      # human answers upfront
    sop_text     = read_docx(sop_path)
    steps        = SOPParser().forward(sop_text, file_context).steps

    executor = StepExecutor(tools=TOOL_REGISTRY)
    for step in steps:
        executor.execute_step(step, file_context)
        # loop does not advance until human clears both checkpoints
```

---

## `main.py` — Entry Point

```bash
python main.py --sop "path/to/sop.docx" --output "path/to/output.xlsx" --files file1.xlsx file2.xls file3.xml
```

Simple argparse — calls `run_agent(sop_path, output_path, file_paths)`.

---

## `pyproject.toml` — Dependencies

```toml
[project]
dependencies = [
    "dspy-ai",
    "openpyxl",       # xlsx read/write/append + formula manipulation (no pandas)
    "xlrd",           # legacy .xls read-only
    "lxml",           # XML parsing
    "beautifulsoup4", # HTML table extraction from XML content
    "python-docx",    # read .docx SOP files
]
```

> **Note:** `openpyxl` is the only Excel library used throughout. Pandas is explicitly excluded. For `.xls` files (legacy format), `xlrd` reads the data which is then handed off — all writes go through openpyxl on `.xlsx`.

---

## Output File Safety

The user provides the target output file (e.g. the prior month's rollforward xlsx). The agent **never modifies the original**:

1. On startup, copy the output file into an `output/` folder:
   ```
   output/
     EO - Fixed Assets Rollforward 2026_Feb26_YS_YYYYMMDD_HHMMSS.xlsx
   ```
   Timestamp suffix ensures each run produces a distinct copy — no silent overwrite of previous runs.
2. All `append_to_sheet`, `create_sheet`, and `copy_formula` tool calls operate **only on the copy** inside `output/`.
3. All reads (prior month data, formula references, sheet structure) also come from the copy — original is never opened again.
4. At the end of the run, agent tells human the exact path of the output file.

---

## Full Execution Flow

```
main.py
  └── copy output file → output/<name>_<timestamp>.xlsx    ← original untouched from here on

  └── gather_file_context()
        for each file → ask_human("What does this file represent?")
        → returns file_context dict

  └── SOPParser(sop_text, file_context)
        → DSPy ChainOfThought → steps[] in order

  └── for each step:
        StepExecutor.plan(step, file_context, tools)
          → DSPy plans ordered tool calls

        for each tool_call in plan:

          preview_action(tool_call)          ← dry_run=True, no file touched
                │
          ask_human("Proceed?")
                │
          no ───┤ feedback → RetryWithFeedback → re-plan → back to top of loop
                │
          yes   ▼
          run_actual(tool_call)              ← dry_run=False, file written

        ask_human("Step complete. Correct?")
                │
          no ───┤ feedback → RetryWithFeedback → re-plan → re-execute full step
                │
          yes   ▼
        advance to next step
```

---

## Formula Copy Design

When copying formulas across periods (e.g. Jan-26 → Feb-26):

- **String substitution** handles sheet name refs, named ranges, date strings in formulas
  - DSPy `BuildFormulaSubstitutions` generates all format variants from source/target period
  - e.g. `"0126" → "0226"`, `"Jan-26" → "Feb-26"`, `"Jan'26" → "Feb'26"`
- **col_offset / row_offset** handles positional shifts when months are laid out column-by-column
- Agent decides which mechanism applies (or both) based on step context
- Always dry-run first — human sees exact formula strings before they are written

---

## Append-to-Sheet Design

Excel sheets often have empty rows after real data (formatting, reserved space). Strategy:

- `find_write_row` scans from top of sheet, finds the last row with any data in the target column range
- Returns `last_data_row + 1` as the write position
- If `after_keyword` is provided, scans only within the block that starts with that header
  - Handles multi-block sheets (e.g. multiple asset types stacked in one tab)
- `append_to_sheet` raises on conflict if any target cell is non-empty — no silent overwrites

---

## Out of Scope (POC)

- NetSuite API calls — steps referencing "In NS navigate to..." are surfaced to human as manual steps, agent skips them
- Overwriting existing Excel cell data
- DSPy compilation / optimization passes
- Concurrent or multi-user runs
- Output validation beyond human sign-off
