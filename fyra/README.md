# Fyra — Financial SOP Automation Agent

Fyra executes financial Standard Operating Procedures (SOPs) step-by-step using an LLM-powered agent with human verification checkpoints. It reads a Word document describing a multi-step process, parses it into executable steps, and carries out each step against an Excel workbook — asking a human for approval before every write.

The name comes from the Swedish word for *four*, reflecting the four-layer design: **parse → plan → execute → verify**.

---

## Thought Process

### Why an agent, not a script?

Financial SOPs are written in natural language for human accountants. The steps say things like *"add a VLOOKUP column matching date-currency pairs from the FX rate table"* — not column indices or formula strings. Hard-coding that into a script would require a separate script per SOP, and any change to the SOP would break it.

An LLM agent can read the intent of each step and decide *how* to accomplish it using available tools — picking the right sheet name, inferring column mappings, choosing the right lookup key format. The agent handles ambiguity; the tools handle correctness.

### Why not use pandas / just write Python?

Two reasons:

1. **Formula preservation.** The output files are handed to accountants who open them in Excel. Pandas would flatten formulas to values. `openpyxl` operates at the cell level and preserves formula strings.

2. **No assumptions about schema.** Pandas requires knowing column names upfront. The agent discovers column names at runtime by reading headers, then builds tool calls accordingly.

### Why DSPy?

DSPy lets us define LLM inputs/outputs as typed Python classes (`Signature`) rather than prompt strings. When the model changes, behaviour stays consistent. It also provides `ReAct` out of the box — the agent loop that alternates between reasoning and tool calls.

### Human-in-the-loop design

Three checkpoints exist deliberately:

1. **File context** — before parsing, confirm what each input file represents.
2. **Pre-write approval** — before every Excel write, show a preview and wait for "yes".
3. **Post-step verification** — after each step, confirm the output looks correct.

This keeps a human accountant in control of every material change while the agent handles the repetitive mechanics. If anything looks wrong, feedback is appended to the step description and the agent retries.

### The bulk-operation principle

Early prototypes sent rows to the LLM for transformation. This hit API payload limits immediately (169k-row exports are ~200 MB of JSON). The redesigned `add_computed_columns` tool reads a 5-row sample for the human preview, gets approval, then does all computation in Python — no row data ever passes through the LLM.

---

## Architecture

```
main.py
  └─ copy output file to output/<name>_<timestamp>.xlsx (original never touched)
  └─ run_agent()
       │
       ├─ STAGE 1 — File context
       │    read_excel() → 5-row sample per file
       │    DSPy InferFileRole → auto-infer file description
       │    gather_file_context() → human confirms or corrects
       │
       ├─ STAGE 2 — SOP parsing
       │    read_docx() → paragraph text
       │    DSPy ParseSOPToSteps → list[SOPStep]
       │
       └─ STAGE 3 — Step execution loop
            for each step:
              DSPy ReAct(ExecuteSOPStep, tools, max_iters=20)
                ↕ tool calls (read, write, compute)
                  write tools → verify_before_write() → human approval
              verify_step_completion() → human check
              if rejected → append feedback → retry step
```

---

## Source Files

| File | Purpose |
|---|---|
| `main.py` | Entry point, CLI, logging setup, output file copy |
| `agent.py` | Core orchestration: file inference, SOP parsing, step execution loop, tool factory |
| `sop_parser.py` | Reads `.docx` and converts to ordered `SOPStep` list via DSPy |
| `signatures.py` | All DSPy Signatures (typed LLM contracts — no raw prompts anywhere) |
| `tools.py` | All tool implementations: Excel read/write, XML parsing, computed columns |
| `human_loop.py` | All human-in-the-loop interactions: file context, pre-write approval, post-step check |

---

## Tools Available to the Agent

### Read tools (no approval needed)

| Tool | What it does |
|---|---|
| `read_excel_tool` | Returns `{total_rows, columns, rows[25]}` — schema + sample only, never full data |
| `list_sheets_tool` | Lists all sheet names in an Excel file |
| `read_xml_tool` | Parses SpreadsheetML or plain XML; returns sheet names or tag summary |
| `extract_html_tables_tool` | Extracts HTML tables from NetSuite XML reports; tracks section headers |
| `build_formula_substitutions_tool` | Generates all date/period format variants (e.g. `Jan-26 → Feb-26`, `0126 → 0226`) |

### Write tools (require human approval)

| Tool | What it does |
|---|---|
| `append_to_sheet_tool` | Bulk-appends rows to a sheet; maps columns by header name |
| `create_sheet_tool` | Creates a new sheet; skips silently if it already exists |
| `copy_formula_tool` | Copies a cell range with period substitutions and col/row offsets applied |
| `add_computed_columns_tool` | Adds computed columns in pure Python — concat, vlookup, multiply, flag |

### `add_computed_columns` expression types

```json
[
  {"name": "Formula Date", "expr": "concat",
   "args": {"fields": ["DOCUMENTDATE", "CURRENCYCODE"],
            "sep": "-", "date_cols": {"DOCUMENTDATE": "%-m-%d-%Y"}}},

  {"name": "FX Rate", "expr": "vlookup",
   "args": {"key_col": "Formula Date",
            "lookup_sheet": "NS Daily FX Rates",
            "lookup_key_col": "Date", "lookup_value_col": "Exchange Rate",
            "default": ""}},

  {"name": "USD Sales Amount", "expr": "multiply",
   "args": {"col_a": "FX Rate", "col_b": "LINEAMOUNT"}},

  {"name": "US Flag", "expr": "flag_equals",
   "args": {"col": "COUNTRY", "value": "US"}},

  {"name": "Missing FX", "expr": "flag_empty",
   "args": {"col": "FX Rate"}}
]
```

---

## Setup

```bash
# From the fyra/ directory
uv sync          # or: pip install -e .

# Copy .env.example → .env and fill in your API keys
cp ../.env.example ../.env
```

**Required environment variables** (in `../.env`):
```
ANTHROPIC_API_KEY=sk-ant-...
LLM_MODEL=claude-haiku-4-5-20251001   # or any anthropic/ model
```

---

## Running

```bash
# Default: runs the Avalara VAT December 2025 SOP
uv run main.py

# Custom paths
uv run main.py \
  --sop /path/to/my_sop.docx \
  --output /path/to/template.xlsx \
  --files /path/to/input1.xlsx /path/to/input2.xml
```

**What you'll be asked at runtime:**

1. Each input file is shown with an auto-inferred description — press Enter to accept or type a correction.
2. Before each Excel write — a preview is shown; type `yes` to proceed or `no` to reject with feedback.
3. After each step — the agent summarises what it did; type `yes` to continue or `no` to give feedback and retry.

---

## Output & Logs

### Output workbook

At startup, the template file passed via `--output` is **copied** into `fyra/output/` with a timestamp suffix — the original is never modified:

```
fyra/output/VAT-Avalara Sales Tax December 2025 ZalosCopy_20260331_171940.xlsx
```

Every write the agent makes goes into this timestamped copy. Re-running the SOP always creates a fresh copy, so previous runs are preserved side-by-side.

The output file is a standard `.xlsx` workbook. Sheets and columns are added progressively as each step completes. At the end of the run the file can be opened directly in Excel — all formula strings are preserved as-written (not flattened to values).

**Typical sheet layout after a full VAT run:**

| Sheet | Added by | Contents |
|---|---|---|
| `SALES TAX SUMMARY` | Template | Pre-existing summary template |
| `GL Detail by Country` | Template | Pre-existing GL reference |
| `NS Daily FX Rates` | Template | FX rate lookup table (read-only reference) |
| `EU Countries` | Template | EU country lookup table (read-only reference) |
| `SalesTaxDocumentLineExport-ALL` | Step 1 | Full transaction export + computed columns (Formula Date, FX Rate, USD amounts, EU flag, US flag) |
| `PIVOT (CURR)` | Step 2 | Pivot by country + currency |
| `PIVOT (Summary)` | Step 3 | Pivot by country, committed transactions only |
| `AVALARA SALES TAX DETAIL-ALLDEC` | Step 4 | Filtered detail for committed/approved lines |
| `Liability State Summary - CA` | Step 5+ | CA-specific liability breakdown |
| `AU` | Step 5+ | Australia-specific sheet |

### Log file

Each run writes a log to `fyra/logs/run_YYYYMMDD_HHMMSS.log`:

```
fyra/logs/run_20260331_171940.log
```

The log captures everything from our own code at DEBUG level — tool calls, row counts, inferred file descriptions, human decisions, approval outcomes, and errors. Third-party libraries (`httpcore`, `httpx`, `LiteLLM`, `dspy` internals) are suppressed to WARNING so the log stays readable.

**Sample log excerpt:**
```
2026-03-31 17:19:41 | INFO  | agent   | Inferred role for SalesTaxDocumentLineExport-Dec 2025.xlsx: detailed export of 169,878 sales tax line items for December 2025
2026-03-31 17:20:09 | INFO  | agent   | SOP parsed into 7 steps
2026-03-31 17:20:09 | INFO  | agent   | Starting step 1: Currency Validation Check
2026-03-31 17:20:11 | DEBUG | tools   | list_sheets: .../SalesTaxDocumentLineExport-Dec 2025.xlsx
2026-03-31 17:21:13 | DEBUG | tools   | Loaded lookup 'NS Daily FX Rates': 9856 entries
2026-03-31 17:22:02 | INFO  | human_loop | Step 1 write approved.
2026-03-31 17:22:02 | INFO  | tools   | add_computed_columns: reading 169878 rows from .../SalesTaxDocumentLineExport-Dec 2025.xlsx
2026-03-31 17:27:14 | INFO  | tools   | add_computed_columns: wrote 169878 rows, 7 new cols to SalesTaxDocumentLineExport-ALL
2026-03-31 17:27:15 | INFO  | agent   | Step 1 complete.
```

The log file and the output workbook share the same timestamp, so you can always match a log to the exact workbook it produced.

---

## Supported File Formats

| Format | How it's read |
|---|---|
| `.xlsx` | openpyxl |
| `.xls` (NetSuite SpreadsheetML) | lxml (these are XML, not legacy BIFF binary) |
| `.xml` (NetSuite reports) | BeautifulSoup html.parser for embedded HTML tables |
| `.docx` (SOP) | python-docx paragraph extraction |

---

## Limitations

### LLM context window
`read_excel` is capped at 25 sample rows to avoid exceeding the API payload limit. The agent cannot reason about patterns across all rows — it sees the schema and a sample, then describes operations for Python to execute in bulk. Aggregations, pivots, or pattern-detection across the full dataset are outside the agent's reach without a dedicated Python tool.

### Formula evaluation
openpyxl reads formula *strings*, not evaluated values. Columns that contain Excel formulas (e.g. `=VLOOKUP(...)`) are read back as the formula text, not the computed number. The agent must either replicate the formula logic in Python or copy the formula string to the destination.

### One SOP shape at a time
The SOP parser extracts steps from a linear Word document. SOPs with branching logic ("if X then do A, else do B"), conditional approval steps, or steps that reference dynamic cell ranges discovered at runtime are not reliably handled.

### Multi-file output
The agent writes to a single output workbook. If a SOP step requires creating a separate output file, the `_assert_output_path` guard will block it — all writes are constrained to `fyra/output/`.

### Retry loops without memory
When a step is retried after human rejection, the feedback is appended to the step description as plain text. The agent has no structured memory of what it tried before — it re-reads the step from scratch. Long retry chains can cause the ReAct trajectory to exceed `max_iters=20` and fail silently.

### Lookup key sensitivity
`add_computed_columns` vlookup matches keys as exact strings. If the key column in the lookup sheet has trailing spaces, mixed case, or different date serialisation, lookups will silently return the default value. No fuzzy matching.

### Performance on large files
Writing 169k rows back to Excel with openpyxl takes 3–5 minutes. openpyxl loads the entire workbook into memory before saving — a 120 MB xlsx can peak at ~1.5 GB RAM during write. There is no streaming write path.

### Windows date formatting
`%-m` (month without leading zero) is a Linux/macOS strftime flag. On Windows the equivalent is `%#m`. The code falls back to a regex strip, but this has not been tested on Windows.
