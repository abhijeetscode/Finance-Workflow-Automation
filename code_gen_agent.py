import json
import logging
import os
from pathlib import Path

import anthropic
import structlog
from dotenv import load_dotenv

from code_gen_models import BusinessLogicSpec, InputMapping, SheetMatch
from code_gen_tools import (
	ANTHROPIC_TOOLS,
	TOOLS,
	ObservationResult,
	get_file_sample,
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
"""


class CodeGenAgent:
	def __init__(self):
		self.client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
		self.model = os.getenv("LM_MODEL", "claude-haiku-4-5")

	def run(self, input_file_path: str, max_steps: int = 20) -> dict:
		log = logger.bind(file=input_file_path)
		log.info("starting_agent")
		new_file_sample = get_file_sample(input_file_path)

		# Agent state — Pydantic models passed between tools
		spec: BusinessLogicSpec | None = None
		sheet_matches: list[SheetMatch] | None = None
		mapping: InputMapping | None = None
		script_path: str | None = None
		output_files: list[str] = []
		retries = 0

		messages: list[dict] = [
			{
				"role": "user",
				"content": (
					f"Process this new input file: {input_file_path}\n\n"
					f"New file sample:\n{new_file_sample}"
				),
			},
		]

		done = False
		for step in range(max_steps):
			log.info("step", current=step + 1, max=max_steps)

			response = self.client.messages.create(
				model=self.model,
				max_tokens=4096,
				system=SYSTEM_PROMPT,
				messages=messages,
				tools=ANTHROPIC_TOOLS,
				temperature=0.0,
			)

			# Build assistant message content
			assistant_content = response.content
			messages.append({"role": "assistant", "content": assistant_content})

			# Check if there are any tool use blocks
			tool_use_blocks = [b for b in assistant_content if b.type == "tool_use"]

			if not tool_use_blocks:
				text = next((b.text for b in assistant_content if b.type == "text"), "")
				log.warning("no_tool_call", response=text)
				continue

			tool_results = []
			for tool_block in tool_use_blocks:
				fn_name = tool_block.name
				fn_args = tool_block.input
				log.info("tool_call", tool=fn_name, args=fn_args)

				if fn_name not in TOOLS:
					result = f"Unknown tool: {fn_name}"
					log.error("unknown_tool", tool=fn_name)
				else:
					try:
						observation = self._dispatch_tool(
							fn_name,
							fn_args,
							input_file_path=input_file_path,
							new_file_sample=new_file_sample,
							spec=spec,
							sheet_matches=sheet_matches,
							mapping=mapping,
							script_path=script_path,
						)
						result = observation.output

						# Update state from tool results
						if fn_name == "learn_business_logic" and observation.success:
							spec = observation.data

						if fn_name == "identify_sheets" and observation.success:
							sheet_matches = observation.data

						if fn_name == "map_columns" and observation.success:
							mapping = observation.data

						if fn_name == "generate_code" and observation.success:
							script_path = observation.data

						if fn_name == "fix_code" and observation.success:
							script_path = observation.data

						if fn_name == "execute_code" and observation.success:
							output_files = observation.data or []
							log.info("output_files", files=output_files)

						if fn_name == "execute_code" and not observation.success:
							retries += 1
							if retries >= 3:
								result += "\nMax retries reached (3). Consider terminating."

						if fn_name == "finish":
							log.info(
								"agent_finished",
								summary=result,
								output_files=output_files,
							)
							done = True

						if fn_name == "terminate":
							log.warning("agent_terminated", reason=result)
							done = True

						if observation.success:
							log.info("observation_ok", tool=fn_name, output=result[:500])
						else:
							log.error("observation_failed", tool=fn_name, output=result[:500])

					except Exception as e:
						result = f"Tool raised an exception: {e}"
						log.exception("tool_exception", tool=fn_name, args=fn_args)

				tool_results.append(
					{
						"type": "tool_result",
						"tool_use_id": tool_block.id,
						"content": result,
					}
				)

			messages.append({"role": "user", "content": tool_results})

			if done:
				break

		return {
			"messages": messages[-1],
			"output_files": output_files,
		}

	def _dispatch_tool(
		self,
		fn_name: str,
		fn_args: dict,
		*,
		input_file_path: str,
		new_file_sample: str,
		spec: BusinessLogicSpec | None,
		sheet_matches: list[SheetMatch] | None,
		mapping: InputMapping | None,
		script_path: str | None,
	) -> ObservationResult:
		"""Dispatch tool call with injected state."""

		if fn_name == "learn_business_logic":
			return TOOLS[fn_name]()

		if fn_name == "identify_sheets":
			return TOOLS[fn_name](spec=spec, new_file_sample=new_file_sample)

		if fn_name == "map_columns":
			return TOOLS[fn_name](
				spec=spec,
				sheet_matches=sheet_matches,
				new_file_path=input_file_path,
			)

		if fn_name == "generate_code":
			return TOOLS[fn_name](
				spec=spec,
				mapping=mapping,
				new_file_sample=new_file_sample,
				user_instructions=fn_args.get("user_instructions", "none"),
			)

		if fn_name == "execute_code":
			if not script_path:
				return ObservationResult(
					tool="execute_code",
					success=False,
					output="No script generated yet. Call generate_code first.",
				)
			return TOOLS[fn_name](
				script_path=script_path,
				input_file=input_file_path,
			)

		if fn_name == "validate_output":
			return TOOLS[fn_name](
				spec=spec,
				mapping=mapping,
				input_file=input_file_path,
			)

		if fn_name == "fix_code":
			if not script_path:
				return ObservationResult(
					tool="fix_code",
					success=False,
					output="No script to fix. Call generate_code first.",
				)
			return TOOLS[fn_name](
				script_path=script_path,
				error_message=fn_args.get("error_message", ""),
				spec=spec,
				mapping=mapping,
			)

		# Simple tools: ask_human, finish, terminate
		return TOOLS[fn_name](**fn_args)


if __name__ == "__main__":
	import sys

	input_file = (
		sys.argv[1] if len(sys.argv) > 1 else "./inputs/[Simple] Bill processing_tampered_2.xlsx"
	)
	agent = CodeGenAgent()
	op = agent.run(input_file_path=input_file)
	print("------ Final Agent Output ------")
	print(json.dumps(op["messages"], indent=2))
