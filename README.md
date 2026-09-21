# Finance Workflow Automation

An agentic system for finance operations. An **Orchestrator** (LangGraph) reads a user objective plus a set of files, routes the work to the right specialist sub-agent, and evaluates the result — retrying or asking the user when needed. Two sub-agents currently exist:

- **CodeGen Agent** — learns business logic from a working example (input file + processing script + output file), then generates and runs Python scripts to transform new Excel inputs into a fixed output format (e.g. vendor payment audits for Bill.com).
- **SOP Agent** — parses a Standard Operating Procedure document (`.docx`) plus supporting data files and executes the steps it describes (e.g. a fixed asset rollforward).

## How It Works

### Orchestrator

```
User objective + files
         │
         ▼
┌───────────────────┐
│ 1. Plan           │  LLM picks exactly one sub-agent (codegen_agent | sop_agent)
│    (planner)      │  and assembles the files/instructions for that step
├───────────────────┤
│ 2. Execute        │  Invoke the chosen sub-agent
│    (executor)     │
├───────────────────┤
│ 3. Evaluate       │  Classify SUCCESS/FAILURE from the sub-agent's output;
│    (evaluator)    │  retry (up to 2x) on transient failures, else stop
├───────────────────┤
│ 4. Log run        │  Fingerprint input files (content shape, not filename)
│    (memory)       │  and persist objective/agent/outcome to memory/
├───────────────────┤
│ 5. Follow-up chat │  After completion, keep the session open for
│                   │  further user turns (checkpointed with thread_id)
└───────────────────┘
```

### CodeGen Agent

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

The CodeGen Agent's output format is fixed:
- **Excel audit file** with sheets: Vendor Payments, Raw Data, Payment Holds, Previously Uploaded
- **CSV file** for Bill.com upload

Input files can have different sheet names, column names, and structure. The agent adapts.

### SOP Agent

Given a `.docx` SOP and supporting files (trial balances, depreciation schedules, output templates, etc.), the agent locates the SOP document, extracts its numbered/bulleted steps, and reports an execution-ready plan referencing the supporting files it was given.

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

### Run the orchestrator

```bash
uv run orchestrator/run_orchestrator.py
```

`run_orchestrator.py` currently hardcodes which scenario to run (`"sop"` or `"vendor_payments"`, see `SCENARIOS` in the file) and feeds it to `orchestrator.agent.main` as if typed on the CLI. To run it directly with your own objective and files:

```bash
uv run python -m orchestrator.agent "transform vendor payments into standard audit format" "./inputs/[Simple] Bill processing_tampered_2.xlsx"
```

After the initial plan → execute → evaluate pass completes, the orchestrator drops into a follow-up chat loop (`quit`/`exit`/`q` to leave).

### Run the CodeGen Agent directly

```bash
# Default input file
uv run codegen_agent/agent.py

# Custom input file
uv run codegen_agent/agent.py "./inputs/[Simple] Bill processing_tampered.xlsx"
```

Top-level `code_gen_agent.py`, `code_gen_models.py`, and `code_gen_tools.py` are the original standalone versions of the same agent, kept at the repo root; `codegen_agent/` is the packaged version imported by the orchestrator (`from codegen_agent import CodeGenAgent`) and exposed via the `codegen-agent` script entry point.

### Schema Adapter Agent (Alternative Approach)

`schema_adapter_agent.py` is a simpler, earlier approach that normalizes input files instead of generating code. It uses OpenAI (GPT-4o-mini) and DSPy to extract a golden schema from `process.py`, then resolves structural drift between new input files and the expected schema. The tool chain (`tool_funcs.py`) detects sheet/column renames via confidence-scored mappings, normalizes the file using pandas (renaming sheets and columns to match the golden schema), and runs the original `process.py` on the normalized file. This works when the input structure is close to the expected format but breaks when the business logic itself needs to change — which is why the Code Generation Agent was built.

## Project Structure

```
├── orchestrator/                # LangGraph orchestrator (routes to sub-agents)
│   ├── agent.py                 # planner / executor / evaluator graph + CLI
│   ├── run_orchestrator.py      # hardcoded scenario runner (dev convenience)
│   ├── sop_agent.py             # SOP document parsing + step extraction
│   ├── tools/
│   │   ├── subagents.py         # tools: invoke_codegen_agent, invoke_sop_agent, log_run, ask_user
│   │   ├── document.py          # .docx section listing/reading tools
│   │   └── excel.py             # Excel sheet listing/reading tools
│   └── memory/
│       ├── store.py             # persist/query run history (orchestrator_runs.json)
│       └── fingerprint.py       # content-based file fingerprinting (sheet/column shape, docx headings)
├── codegen_agent/                # packaged CodeGen Agent (used by the orchestrator)
│   ├── agent.py                  # agent loop (Anthropic Messages API)
│   ├── tools.py                  # tool implementations (DSPy + pandas)
│   └── models.py                 # Pydantic models for data transfer
├── code_gen_agent.py             # original standalone CodeGen Agent loop
├── code_gen_tools.py             # original standalone tool implementations
├── code_gen_models.py            # original standalone Pydantic models
├── schema_adapter_agent.py       # Schema Adapter Agent (OpenAI, alternative approach)
├── tool_funcs.py                 # Schema Adapter tool implementations
├── code/
│   └── process.py                # reference processing script (ground truth for CodeGen Agent)
├── inputs/                       # input Excel files (normal + tampered + error) for CodeGen Agent
├── outputs/                      # generated output files (audit xlsx + Bill.com csv)
├── generated_scripts/            # LLM-generated transform scripts
├── raw_data/                     # SOP Agent inputs (SOP docx, trial balance, fixed asset reports, etc.)
├── sop_outputs/                  # SOP Agent output files
├── logs/                         # structured logs (structlog) — orchestrator, codegen, schema adapter
├── memory/                       # orchestrator run history (orchestrator_runs.json)
├── business_logic_spec.json      # cached BusinessLogicSpec (for CodeGen Agent)
├── orchestrator_graph.png        # rendered LangGraph graph for the orchestrator
└── deployment_architecture.excalidraw  # architecture diagram
```

## Architecture

### Orchestrator

`orchestrator/agent.py` builds a LangGraph `StateGraph` with three nodes:

- **planner** — structured-output LLM call that reads the objective and file list and produces exactly one `PlanStep` (`agent`, `files`, `instructions`). It does not chain multiple sub-agents in one plan.
- **executor** — invokes the chosen sub-agent's tool (`invoke_codegen_agent` or `invoke_sop_agent`) with the planned files/instructions.
- **evaluator** — structured-output LLM call that classifies the step as success/failure from the sub-agent's output prefix (`SUCCESS:` / `FAILURE:`) or content, decides whether to retry (up to `MAX_RETRIES = 2`, only for transient failures), and on completion logs the run to memory.

State is checkpointed (`MemorySaver`, keyed by a session `thread_id`), so after the initial run finishes the CLI keeps the graph alive for follow-up turns.

### CodeGen Agent

`codegen_agent/agent.py` (and its standalone twin `code_gen_agent.py`) runs a tool-calling loop using the Anthropic Messages API. The LLM decides which tool to call next based on a system prompt that defines the workflow. Agent state (spec, mapping, script path) is tracked as Pydantic models and injected into tool calls.

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

### SOP Agent

`orchestrator/sop_agent.py` is deterministic (no LLM call): it finds the `.docx` file among the given inputs, extracts paragraphs that look like numbered/lettered/bulleted steps (falling back to the first several substantial paragraphs if no clear step markers exist), and returns a structured preview referencing the supporting files. `orchestrator/tools/document.py` and `orchestrator/tools/excel.py` provide LangChain tools (section listing/reading, sheet listing/reading) available for a more LLM-driven SOP execution in the future.

### Memory

`orchestrator/memory/store.py` persists every orchestrator run (files, intent, agent used, outcome, timestamp) to `memory/orchestrator_runs.json`. Files are identified by a **content fingerprint** (`orchestrator/memory/fingerprint.py`) rather than filename — for `.xlsx` it hashes sorted sheet names + column headers, for `.docx` it hashes sorted heading text — so renamed-but-structurally-identical files still match. `find_similar_runs` can surface prior runs against similar files for future context injection.

### Key Design Decisions

- **Output format is fixed, input varies** — the CodeGen Agent adapts to new input structures while always producing the same output schema
- **One sub-agent per objective** — the orchestrator's planner deliberately avoids chaining agents within a single plan, keeping routing decisions simple and auditable
- **Pydantic models for state** — no JSON serialization round-trips between CodeGen Agent tools
- **Confidence rubric** — deterministic scoring rules for sheet/column matching, not just LLM vibes
- **fix_code gets the mapping** — when a generated script fails, the fixer sees the actual sheet/column names, not just semantic names
- **Content fingerprinting over filenames** — orchestrator memory matches files by structural shape so renamed inputs still hit prior run history
- **Structured logging** — structlog writes to `logs/orchestrator.log`, `logs/code_gen_agent.log`, and `logs/schema_adapter_agent.log` for observability

### Alerting

Metrics to monitor and when to fire alerts:

```
                    ┌─────────────────────────────────────┐
                    │         Agent Health Dashboard       │
                    └─────────────────────────────────────┘

  Success Rate (rolling 1h)          Retry Rate                  Latency (p95)
  ┌──────────────────────┐     ┌──────────────────────┐    ┌──────────────────────┐
  │ ████████████████ 95% │     │ ██░░░░░░░░░░░░░  8% │    │ ████████░░░░░░ 4min  │
  │                      │     │                      │    │                      │
  │ WARN < 80%           │     │ WARN > 30%           │    │ WARN > 8min          │
  │ CRIT < 60%           │     │ CRIT > 50%           │    │ CRIT > 12min         │
  └──────────────────────┘     └──────────────────────┘    └──────────────────────┘

  Human Asks / Job               Terminate Rate              Token Spend (daily)
  ┌──────────────────────┐     ┌──────────────────────┐    ┌──────────────────────┐
  │ █░░░░░░░░░░░░░░ 1.2  │     │ ██░░░░░░░░░░░░░ 10% │    │ ██████░░░░░░░░ $12   │
  │                      │     │                      │    │                      │
  │ WARN > 3             │     │ WARN > 25%           │    │ WARN > $50           │
  │ CRIT > 5             │     │ CRIT > 40%           │    │ CRIT > $100          │
  └──────────────────────┘     └──────────────────────┘    └──────────────────────┘
```

| Metric | Source | WARN | CRIT | Why |
|--------|--------|------|------|-----|
| Success rate | `finish` vs `terminate` count (CodeGen Agent) / evaluator `status` (orchestrator) | < 80% | < 60% | Agent failing too often, spec or prompts may need tuning |
| Retry rate | `fix_code` calls / total jobs, or orchestrator retries / total steps | > 30% | > 50% | Generated code quality degrading, check LLM model changes |
| Latency p95 | `started_at` to `finished_at` | > 8min | > 12min | LLM slowdown, queue backlog, or script stuck in loop |
| Human asks / job | `ask_human` calls per job | > 3 | > 5 | Confidence too low, mapping logic needs improvement |
| Terminate rate | `terminate` / total jobs, or orchestrator `failed` status rate | > 25% | > 40% | Bad input files increasing, or agent too conservative |
| Token spend | Sum of `input_tokens + output_tokens` across orchestrator + sub-agents | > $50/day | > $100/day | Runaway retries or prompt bloat |

### Deployment (Scaled)

See `deployment_architecture.excalidraw` for the full diagram. Key components:

- **Script Cache Layer** — Vector DB (ChromaDB) stores file fingerprints → generated scripts. Similar files reuse cached scripts instead of running the full agent loop.
- **FastAPI + Load Balancer** — API layer for file upload, human responses, output download
- **Job Queue (Redis/SQS)** — decouples API from workers
- **Worker Pool (ECS)** — stateless workers with checkpointing for human-in-the-loop pause/resume
- **Sandboxed Execution** — generated scripts run in Docker containers (no network, memory-limited)
- **S3** — file storage for inputs, outputs, generated scripts
- **Postgres** — job state, checkpoints, spec versions
