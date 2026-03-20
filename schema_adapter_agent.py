import os
import json
import logging
import dspy
import pandas as pd
from openai import OpenAI
from pydantic import BaseModel
from pathlib import Path
from tabulate import tabulate
from dotenv import load_dotenv
from tool_funcs import TOOLS, OPENAI_TOOLS, ObservationResult

load_dotenv()

LOG_DIR = Path("./logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "schema_adapter_agent.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("SchemaAdapterAgent")


# ── Pydantic Models ───────────────────────────────────────────────────────────


class ColumnSchema(BaseModel):
    name: str
    purpose: str
    example_values: list[str]
    critical: bool


class SheetSchema(BaseModel):
    name: str
    purpose: str
    columns: list[ColumnSchema]


class GoldenSchema(BaseModel):
    sheets: list[SheetSchema]


# ── DSPy Signature (golden schema extraction only) ───────────────────────────


class SchemaResolutionSignature(dspy.Signature):
    """
    Given a working execution agent and the file it was built against,
    understand the semantic meaning of each sheet and column — not just
    their names, but what business purpose they serve in the process.
    """

    source_code: str = dspy.InputField(
        description="Full source code of the execution agent"
    )
    working_file_sample: str = dspy.InputField(
        description="Tabular string sample from the file that the execution agent "
        "works correctly on, showing sheet name followed by rows of data"
    )
    golden_schema: GoldenSchema = dspy.OutputField(
        description="Semantic understanding of each sheet and column including "
        "purpose and criticality"
    )


# ── OpenAI Tool Definitions ──────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a schema resolution agent for finance automation.
You are expert in detecting the semantic meaning and business purpose of different sheets and columns in Excel files related to bill processing, such as invoices, payment records, and vendor information.
You have access to golden schema extracted from the original working file, which describes the purpose and criticality of each sheet and column.
When given a new input file sample, your task is to identify how the new file's structure maps to the golden schema,
and determine if any normalization steps are needed to align the new file with the golden schema.

Decision rules based on confidence scores from resolve_schema:
- High confidence (>=0.70): proceed with normalize_file, then execute_agent, then call finish
- Medium confidence (0.50-0.70): call ask_human for confirmation before proceeding
- Low confidence (<0.50) or domain_match is false: call terminate with explanation

If critical fields are missing, call terminate with an explanation.
You MUST end every run by calling either finish (success) or terminate (failure).
"""


# ── Agent ─────────────────────────────────────────────────────────────────────


class SchemaAdapterAgent:
    def __init__(self):
        self.openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.lm = dspy.LM(
            "gpt-4o-mini",
            temperature=0.0,
            api_key=os.getenv("OPENAI_API_KEY"),
        )
        dspy.configure(lm=self.lm)
        self.resolve_step = dspy.ChainOfThought(SchemaResolutionSignature)
        self.golden_schema_path: Path = Path("./golden_schema.json")

    @staticmethod
    def _get_source_code() -> str:
        return Path("./code/process.py").read_text()

    @staticmethod
    def _get_file_sample(file_path: str, nrows: int = 5) -> str:
        xl = pd.ExcelFile(file_path)
        result = []
        for sheet_name in xl.sheet_names:
            df = pd.read_excel(xl, sheet_name=sheet_name)
            data_sample = df.sample(min(nrows, len(df)))
            result.append(f"Sheet: {sheet_name}")
            result.append(
                tabulate(data_sample, headers="keys", tablefmt="pipe", showindex=False)
            )
            result.append("")
        return "\n".join(result)

    def build_golden_schema(self) -> GoldenSchema:
        """Run once against the working file and cache the result to disk."""
        if self.golden_schema_path.exists():
            logger.info("Loading golden schema from %s", self.golden_schema_path)
            return GoldenSchema.model_validate_json(self.golden_schema_path.read_text())

        logger.info("Building golden schema from working file...")
        output = self.resolve_step(
            source_code=self._get_source_code(),
            working_file_sample=self._get_file_sample(
                "./inputs/[Simple] Bill processing.xlsx"
            ),
        )
        schema = output.golden_schema
        self.golden_schema_path.write_text(schema.model_dump_json(indent=2))
        logger.info("Golden schema saved to %s", self.golden_schema_path)
        return schema

    def run(self, input_file_path: str, max_steps: int = 10) -> list[dict]:
        """Run the agent loop using OpenAI native tool calling."""
        logger.info("Starting agent loop for file: %s", input_file_path)
        golden_schema = self.build_golden_schema()
        new_file_sample = self._get_file_sample(input_file_path)
        normalized_path: str | None = None
        drift_resolution = None

        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Process this new input file.\n\n"
                    f"Golden schema:\n{golden_schema.model_dump_json()}\n\n"
                    f"New file sample:\n{new_file_sample}"
                ),
            },
        ]

        done = False
        for step in range(max_steps):
            logger.info("Step %d/%d", step + 1, max_steps)

            response = self.openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                tools=OPENAI_TOOLS,
                temperature=0.0,
            )

            msg = response.choices[0].message
            messages.append(msg.model_dump(exclude_none=True))

            if not msg.tool_calls:
                logger.warning(
                    "Agent responded without calling a tool: %s", msg.content
                )
                continue

            for tool_call in msg.tool_calls:
                fn_name = tool_call.function.name
                fn_args = json.loads(tool_call.function.arguments)
                logger.info(
                    "Tool call: %s args=%s", fn_name, json.dumps(fn_args, indent=2)
                )

                if fn_name not in TOOLS:
                    result = f"Unknown tool: {fn_name}"
                    logger.error(result)
                else:
                    try:
                        if fn_name == "normalize_file":
                            fn_args["input_file"] = input_file_path
                            fn_args["drift_resolution"] = drift_resolution
                            logger.info(
                                "Injected input_file for normalize_file: %s",
                                input_file_path,
                            )

                        if fn_name == "execute_agent":
                            if not normalized_path:
                                result = (
                                    "normalize_file must be called before execute_agent"
                                )
                                logger.error(result)
                                messages.append(
                                    {
                                        "role": "tool",
                                        "tool_call_id": tool_call.id,
                                        "content": result,
                                    }
                                )
                                continue
                            fn_args["input_file"] = normalized_path
                            logger.info(
                                "Injected input_file for execute_agent: %s",
                                normalized_path,
                            )

                        observation = TOOLS[fn_name](**fn_args)
                        result = observation.output

                        if fn_name == "resolve_schema" and observation.success:
                            drift_resolution = observation.data

                        if fn_name == "normalize_file" and observation.success:
                            normalized_path = observation.data

                        if fn_name == "finish":
                            logger.info("Agent finished: %s", result)
                            done = True

                        if fn_name == "terminate":
                            logger.warning("Agent terminated: %s", result)
                            done = True

                        if observation.success:
                            logger.info(
                                "Observation [%s] OK: %s", fn_name, result[:300]
                            )
                        else:
                            logger.error(
                                "Observation [%s] FAILED: %s", fn_name, result[:300]
                            )

                    except Exception as e:
                        result = f"Tool raised an exception: {e}"
                        logger.error(
                            "Tool %s raised exception: %s\nArgs: %s",
                            fn_name,
                            e,
                            json.dumps(fn_args, indent=2),
                            exc_info=True,
                        )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    }
                )

            if done:
                break

        return messages


if __name__ == "__main__":
    agent = SchemaAdapterAgent()
    agent.run(input_file_path="./inputs/[Simple] Bill processing copy.xlsx")
