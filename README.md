# Code Generation Agent for Finance Automation

An agentic system that learns business logic from a working example (input file + processing script + output file), then generates Python scripts to transform new input files into the expected output format.

## How It Works

```
New Excel file
     │
     ▼
┌─────────────────┐
│ 1. Learn         │  Extract BusinessLogicSpec from reference files
│    (cached)      │  (process.py + known input + known output)
├─────────────────┤
│ 2. Identify      │  Match sheets by content, not name
│    Sheets        │  (semantic role matching with confidence scoring)
├─────────────────┤
│ 3. Map Columns   │  Map columns in each sheet to expected roles
│                  │  (confidence rubric: HIGH/MEDIUM/LOW)
├─────────────────┤
│ 4. Ask Human     │  Confirm MEDIUM confidence mappings
│    (if needed)   │  Ask for additional context
├─────────────────┤
│ 5. Generate      │  Generate a Python script that transforms
│    Code          │  input → fixed output format
├─────────────────┤
│ 6. Execute       │  Run generated script in subprocess
├─────────────────┤
│ 7. Validate      │  Check output against BusinessLogicSpec
│                  │  (retry with fix_code up to 3 times)
├─────────────────┤
│ 8. Finish        │  Output files in ./outputs/
└─────────────────┘
```

The output format is fixed:
- **Excel audit file** with sheets: Vendor Payments, Raw Data, Payment Holds, Previously Uploaded
- **CSV file** for Bill.com upload

Input files can have different sheet names, column names, and structure. The agent adapts.

## Setup

```bash
# Install dependencies (requires Python 3.12+)
uv sync

# Set environment variables
cp .env.example .env
# Edit .env with your API key:
#   ANTHROPIC_API_KEY=sk-ant-...
#   LM_MODEL=claude-haiku-4-5
```

## Usage

### Run the agent

```bash
# Default input file
uv run code_gen_agent.py

# Custom input file
uv run code_gen_agent.py "./inputs/[Simple] Bill processing_tampered.xlsx"
```

### Schema Adapter Agent (Alternative Approach)

`schema_adapter_agent.py` is a simpler, earlier approach that normalizes input files instead of generating code. It uses OpenAI (GPT-4o-mini) and DSPy to extract a golden schema from `process.py`, then resolves structural drift between new input files and the expected schema. The tool chain (`tool_funcs.py`) detects sheet/column renames via confidence-scored mappings, normalizes the file using pandas (renaming sheets and columns to match the golden schema), and runs the original `process.py` on the normalized file. This works when the input structure is close to the expected format but breaks when the business logic itself needs to change — which is why the Code Generation Agent was built.

## Project Structure

```
├── code_gen_agent.py       # Code Gen Agent loop (Anthropic Messages API)
├── code_gen_tools.py       # Code Gen tool implementations (DSPy + pandas)
├── code_gen_models.py      # Pydantic models for data transfer
├── schema_adapter_agent.py # Schema Adapter Agent (OpenAI, alternative approach)
├── tool_funcs.py           # Schema Adapter tool implementations
├── code/
│   └── process.py          # Reference processing script (ground truth)
├── inputs/                 # Input Excel files (normal + tampered + error)
├── outputs/                # Generated output files
├── generated_scripts/      # LLM-generated transform scripts
├── logs/                   # Structured logs (structlog)
├── golden_schema.json      # Expected input schema (for Schema Adapter)
├── business_logic_spec.json # Cached BusinessLogicSpec (for Code Gen Agent)
└── deployment_architecture.excalidraw  # Architecture diagram
```

## Architecture

### Agent Loop

`code_gen_agent.py` runs a tool-calling loop using the Anthropic Messages API. The LLM decides which tool to call next based on a system prompt that defines the workflow. Agent state (spec, mapping, script path) is tracked as Pydantic models and injected into tool calls.

### Tools

| Tool | Purpose |
|------|---------|
| `learn_business_logic` | 3 DSPy calls to extract BusinessLogicSpec from reference files. Cached to disk. |
| `identify_sheets` | LLM-based semantic matching of sheet names with confidence scoring |
| `map_columns` | Per-sheet column mapping with confidence rubric (HIGH/MEDIUM/LOW) |
| `generate_code` | DSPy structured output → complete Python transform script |
| `execute_code` | Run generated script in subprocess, detect output files |
| `validate_output` | Check output against spec (sheets, columns, row counts, semantic rules) |
| `fix_code` | Fix failed scripts using error message + input mapping context |
| `ask_human` | Pause for human input (confirmation or additional context) |
| `finish` / `terminate` | End the agent loop (success / failure) |

### Key Design Decisions

- **Output format is fixed, input varies** — the agent adapts to new input structures while always producing the same output schema
- **Pydantic models for state** — no JSON serialization round-trips between tools
- **Confidence rubric** — deterministic scoring rules for sheet/column matching, not just LLM vibes
- **fix_code gets the mapping** — when a generated script fails, the fixer sees the actual sheet/column names, not just semantic names
- **Structured logging** — structlog writes to `logs/code_gen_agent.log` for observability

### Deployment (Scaled)

See `deployment_architecture.excalidraw` for the full diagram. Key components:

- **Script Cache Layer** — Vector DB (ChromaDB) stores file fingerprints → generated scripts. Similar files reuse cached scripts instead of running the full agent loop.
- **FastAPI + Load Balancer** — API layer for file upload, human responses, output download
- **Job Queue (Redis/SQS)** — decouples API from workers
- **Worker Pool (ECS)** — stateless workers with checkpointing for human-in-the-loop pause/resume
- **Sandboxed Execution** — generated scripts run in Docker containers (no network, memory-limited)
- **S3** — file storage for inputs, outputs, generated scripts
- **Postgres** — job state, checkpoints, spec versions
