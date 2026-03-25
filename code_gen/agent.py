import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
import structlog
from dotenv import load_dotenv
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.fallback import FallbackModel

from code_gen.models import BusinessLogicSpec, InputMapping, SheetMatch
from code_gen.tools import (
	ObservationResult,
	get_file_sample,
	tool_ask_human,
	tool_execute_code,
	tool_finish,
	tool_fix_code,
	tool_generate_code,
	tool_identify_sheets,
	tool_learn_business_logic,
	tool_map_columns,
	tool_terminate,
	tool_validate_output,
)

load_dotenv()

LOG_DIR = Path("./logs")
LOG_DIR.mkdir(exist_ok=True)

structlog.configure(
	processors=[
		structlog.contextvars.merge_contextvars,
		structlog.processors.add_log_level,
		structlog.processors.TimeStamper(fmt="iso"),
		structlog.dev.ConsoleRenderer(colors=False),
	],
	wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
	logger_factory=structlog.WriteLoggerFactory(
		file=open(LOG_DIR / "code_gen_agent.log", "a"),  # noqa: SIM115
	),
)
logger = structlog.get_logger("CodeGenAgent")


SYSTEM_PROMPT = """You are a code generation agent for finance automation.
You understand business logic by learning from a working example (input file + processing script + output file),
then generate Python scripts to transform new input files into the expected output format.

The output format is FIXED — it must always produce:
- An Excel audit file with sheets: "Vendor Payments", "Raw Data", "Payment Holds", "Previously Uploaded"
- Exact column names and structure as defined in the BusinessLogicSpec
The input file structure may vary (different sheet names, column names), but the output must always match the expected format.

Your workflow:
1. Call learn_business_logic to extract the BusinessLogicSpec (done once, cached)
2. Call identify_sheets to match new file sheets to expected semantic roles
3. Call map_columns to map columns in each identified sheet
4. Review the mapping confidence:
   - If any critical mapping is MEDIUM confidence: call ask_human to confirm
   - If any critical mapping is LOW confidence: call terminate
5. Call ask_human to ask the user if they have any additional context about this file
6. Call generate_code with any user instructions
7. Call execute_code to run the generated script
8. Call validate_output to check the result
   - If validation fails: call fix_code with the error, then execute_code again (max 3 retries)
9. Call finish when done, or terminate if it cannot be completed

You MUST end every run by calling either finish (success) or terminate (failure).

IMPORTANT rules for ask_human:
- The user is a non-technical business person. Do NOT use internal jargon like "semantic role", "confidence score", "mapping", "spec", or column/sheet IDs.
- Write questions in plain English. Explain what you found and what you need confirmed.
- Example BAD question: "The column mapping for 'Payment Hold List.Vendor Name' has MEDIUM confidence (0.55). Confirm?"
- Example GOOD question: "In sheet 'Sheet 2', I found a column called 'Vendor' — does this contain the list of vendors whose payments should be held?"
- NEVER call ask_human and other tools in parallel. Always wait for the human response before proceeding.
- NEVER call ask_human more than once per turn. You MUST only make ONE tool call when that call is ask_human.
- If you have multiple questions, combine them into a single numbered list in ONE ask_human call.
- After ask_human returns, read the response before deciding the next step.
"""


@dataclass
class AgentDeps:
	input_file_path: str
	new_file_sample: str
	spec: BusinessLogicSpec | None = None
	sheet_matches: list[SheetMatch] | None = None
	mapping: InputMapping | None = None
	script_path: str | None = None
	output_files: list[str] = field(default_factory=list)
	retries: int = 0
	step: int = 0
	done: bool = False
	terminated: bool = False
	terminate_reason: str = ""
	_ask_human_pending: bool = False
	ask_human_fn: Callable[[str], Awaitable[ObservationResult]] | None = None
	on_tool_start: Callable[[int, str], Awaitable[None]] | None = None
	on_step: Callable[[int, str, ObservationResult], Awaitable[None]] | None = None


def _build_model():
	"""Build model with FallbackModel if both keys are available."""
	anthropic_key = os.getenv("ANTHROPIC_API_KEY")
	openai_key = os.getenv("OPENAI_API_KEY")
	lm_model = os.getenv("LM_MODEL", "claude-haiku-4-5")

	if openai_key and anthropic_key:
		return FallbackModel(
			"openai:gpt-4o-mini",
			f"anthropic:{lm_model}",
		)
	elif openai_key:
		return "openai:gpt-4o-mini"
	elif anthropic_key:
		return f"anthropic:{lm_model}"
	else:
		return "openai:gpt-4o-mini"


code_gen_agent = Agent(
	model=_build_model(),
	system_prompt=SYSTEM_PROMPT,
	deps_type=AgentDeps,
)


async def _notify_start(ctx: RunContext[AgentDeps], tool_name: str):
	ctx.deps.step += 1
	print(f"[agent] Tool START: {tool_name} (step {ctx.deps.step})")
	if ctx.deps.on_tool_start:
		await ctx.deps.on_tool_start(ctx.deps.step, tool_name)


async def _notify_done(ctx: RunContext[AgentDeps], tool_name: str, obs: ObservationResult):
	print(f"[agent] Tool DONE:  {tool_name} (step {ctx.deps.step}, success={obs.success})")
	if ctx.deps.on_step:
		await ctx.deps.on_step(ctx.deps.step, tool_name, obs)


@code_gen_agent.tool
async def learn_business_logic(ctx: RunContext[AgentDeps]) -> str:
	"""Extract BusinessLogicSpec from working input, process.py, and output. Run once."""
	await _notify_start(ctx, "learn_business_logic")
	obs = await asyncio.to_thread(tool_learn_business_logic)
	if obs.success:
		ctx.deps.spec = obs.data
	await _notify_done(ctx, "learn_business_logic", obs)
	return obs.output


@code_gen_agent.tool
async def identify_sheets(ctx: RunContext[AgentDeps]) -> str:
	"""Identify which sheets in the new file match expected semantic roles. Run after learn_business_logic."""
	await _notify_start(ctx, "identify_sheets")
	obs = await asyncio.to_thread(
		tool_identify_sheets, spec=ctx.deps.spec, new_file_sample=ctx.deps.new_file_sample
	)
	if obs.success:
		ctx.deps.sheet_matches = obs.data
	await _notify_done(ctx, "identify_sheets", obs)
	return obs.output


@code_gen_agent.tool
async def map_columns(ctx: RunContext[AgentDeps]) -> str:
	"""Map columns in each identified sheet to expected semantic columns. Run after identify_sheets."""
	await _notify_start(ctx, "map_columns")
	obs = await asyncio.to_thread(
		tool_map_columns,
		spec=ctx.deps.spec,
		sheet_matches=ctx.deps.sheet_matches,
		new_file_path=ctx.deps.input_file_path,
	)
	if obs.success:
		ctx.deps.mapping = obs.data
	await _notify_done(ctx, "map_columns", obs)
	return obs.output


@code_gen_agent.tool
async def generate_code(ctx: RunContext[AgentDeps], user_instructions: str = "none") -> str:
	"""Generate a Python transformation script. Run after map_columns."""
	await _notify_start(ctx, "generate_code")
	obs = await asyncio.to_thread(
		tool_generate_code,
		spec=ctx.deps.spec,
		mapping=ctx.deps.mapping,
		new_file_sample=ctx.deps.new_file_sample,
		user_instructions=user_instructions,
	)
	if obs.success:
		ctx.deps.script_path = obs.data
	await _notify_done(ctx, "generate_code", obs)
	return obs.output


@code_gen_agent.tool
async def execute_code(ctx: RunContext[AgentDeps]) -> str:
	"""Run the generated transformation script."""
	await _notify_start(ctx, "execute_code")
	if not ctx.deps.script_path:
		return "No script generated yet. Call generate_code first."
	obs = await asyncio.to_thread(
		tool_execute_code,
		script_path=ctx.deps.script_path,
		input_file=ctx.deps.input_file_path,
	)
	if obs.success:
		ctx.deps.output_files = obs.data or []
	else:
		ctx.deps.retries += 1
	result = obs.output
	if not obs.success and ctx.deps.retries >= 3:
		result += "\nMax retries reached (3). Consider terminating."
	await _notify_done(ctx, "execute_code", obs)
	return result


@code_gen_agent.tool
async def validate_output(ctx: RunContext[AgentDeps]) -> str:
	"""Validate the generated output against expected format (structural + semantic)."""
	await _notify_start(ctx, "validate_output")
	obs = await asyncio.to_thread(
		tool_validate_output,
		spec=ctx.deps.spec,
		mapping=ctx.deps.mapping,
		input_file=ctx.deps.input_file_path,
	)
	await _notify_done(ctx, "validate_output", obs)
	return obs.output


@code_gen_agent.tool
async def fix_code(ctx: RunContext[AgentDeps], error_message: str) -> str:
	"""Fix a generated script that failed. Provide the error message."""
	await _notify_start(ctx, "fix_code")
	if not ctx.deps.script_path:
		return "No script to fix. Call generate_code first."
	obs = await asyncio.to_thread(
		tool_fix_code,
		script_path=ctx.deps.script_path,
		error_message=error_message,
		spec=ctx.deps.spec,
		mapping=ctx.deps.mapping,
	)
	if obs.success:
		ctx.deps.script_path = obs.data
	await _notify_done(ctx, "fix_code", obs)
	return obs.output


@code_gen_agent.tool
async def ask_human(ctx: RunContext[AgentDeps], question: str) -> str:
	"""Ask the user a question for clarification or additional context. Only call ONCE per turn, never in parallel with other tools."""
	if ctx.deps._ask_human_pending:
		return "Another ask_human is already waiting. Do NOT call ask_human multiple times. Wait for the previous response first."
	ctx.deps._ask_human_pending = True
	await _notify_start(ctx, "ask_human")
	print(f"[agent] ask_human: waiting for human response... question='{question[:80]}'")
	if ctx.deps.ask_human_fn:
		obs = await ctx.deps.ask_human_fn(question)
	else:
		obs = tool_ask_human(question)
	ctx.deps._ask_human_pending = False
	print(f"[agent] ask_human: got response: '{obs.output[:80]}'")
	await _notify_done(ctx, "ask_human", obs)
	return obs.output


@code_gen_agent.tool
async def finish(ctx: RunContext[AgentDeps], summary: str) -> str:
	"""Signal successful completion."""
	await _notify_start(ctx, "finish")
	obs = tool_finish(summary)
	ctx.deps.done = True
	await _notify_done(ctx, "finish", obs)
	return obs.output


@code_gen_agent.tool
async def terminate(ctx: RunContext[AgentDeps], reason: str) -> str:
	"""Stop processing — file cannot be handled."""
	await _notify_start(ctx, "terminate")
	obs = tool_terminate(reason)
	ctx.deps.done = True
	ctx.deps.terminated = True
	ctx.deps.terminate_reason = reason
	await _notify_done(ctx, "terminate", obs)
	return obs.output


class CodeGenAgent:
	def __init__(
		self,
		ask_human_fn: Callable[[str], Awaitable[ObservationResult]] | None = None,
		on_tool_start: Callable[[int, str], Awaitable[None]] | None = None,
		on_step: Callable[[int, str, ObservationResult], Awaitable[None]] | None = None,
	):
		self.ask_human_fn = ask_human_fn
		self.on_tool_start = on_tool_start
		self.on_step = on_step

	async def run(self, input_file_path: str) -> dict:
		log = logger.bind(file=input_file_path)
		log.info("starting_agent")

		new_file_sample = get_file_sample(input_file_path)

		deps = AgentDeps(
			input_file_path=input_file_path,
			new_file_sample=new_file_sample,
			ask_human_fn=self.ask_human_fn,
			on_tool_start=self.on_tool_start,
			on_step=self.on_step,
		)

		print(f"[agent] Starting PydanticAI agent with model: {code_gen_agent.model}")
		result = await code_gen_agent.run(
			f"Process this new input file: {input_file_path}\n\n"
			f"New file sample:\n{new_file_sample}",
			deps=deps,
		)
		print(f"[agent] Agent finished. Output files: {deps.output_files}")

		log.info("agent_finished", output_files=deps.output_files)

		return {
			"messages": json.loads(result.all_messages_json())[-1],
			"output_files": deps.output_files,
			"terminated": deps.terminated,
			"terminate_reason": deps.terminate_reason,
		}


if __name__ == "__main__":
	import sys

	input_file = (
		sys.argv[1] if len(sys.argv) > 1 else "./inputs/[Simple] Bill processing_tampered_2.xlsx"
	)
	agent = CodeGenAgent()
	op = asyncio.run(agent.run(input_file_path=input_file))
	print("------ Final Agent Output ------")
	print(json.dumps(op["messages"], indent=2))
