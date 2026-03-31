import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Annotated, Literal

import structlog
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel
from typing_extensions import TypedDict

from orchestrator.tools.subagents import ask_user, invoke_codegen_agent, invoke_sop_agent, log_run

load_dotenv("../.env")

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_DIR = Path("./logs")
LOG_DIR.mkdir(exist_ok=True)

structlog.configure(
	processors=[
		structlog.contextvars.merge_contextvars,
		structlog.processors.add_log_level,
		structlog.processors.TimeStamper(fmt="iso"),
		structlog.dev.ConsoleRenderer(colors=False),
	],
	wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
	logger_factory=structlog.WriteLoggerFactory(
		file=open(LOG_DIR / "orchestrator.log", "a"),  # noqa: SIM115
	),
)
logger = structlog.get_logger("Orchestrator")

# ── Structured output models ──────────────────────────────────────────────────


class PlanStep(BaseModel):
	agent: Literal["codegen_agent", "sop_agent"]
	files: list[str]
	instructions: str


class ExecutionPlan(BaseModel):
	step: PlanStep
	reasoning: str


class StepEvaluation(BaseModel):
	success: bool
	retry: bool
	reasoning: str


# ── Prompts ───────────────────────────────────────────────────────────────────

PLANNER_SYSTEM = """You are a routing component of an orchestration system for financial workflows.

Given a user objective and files, choose exactly one agent and produce exactly one execution step.

Available agents:
	- codegen_agent: transforms Excel files (.xlsx) into a fixed audit output
	- sop_agent: executes a business process by following steps in an SOP document

Rules:
- Return exactly one step
- Choose one agent for the whole objective
- Do not chain agents
- Use only the files needed for that agent, in the correct order
- codegen_agent should usually receive exactly one Excel input file
- sop_agent may require an SOP document plus additional supporting files
- If the objective is SOP-driven, route to sop_agent
- If the objective is vendor-payment audit transformation from an Excel file, route to codegen_agent
"""

EVALUATOR_SYSTEM = """You are the evaluation component of an orchestration system for financial workflows.

A sub-agent just completed the only step. You must:
1. Determine success from the output prefix:
   - Output starts with "SUCCESS:" → success = true
   - Output starts with "FAILURE:" → success = false
   - No prefix → infer from content (file paths present = success, exception/error = failure)
2. Decide whether to retry on failure:
   - retry = false if the output explicitly says "do not retry"
   - retry = false if the agent already ran human interactions (it managed its own retries internally)
   - retry = true only for clearly transient failures (e.g. network timeout, unexpected exception)
"""


class OrchestratorState(TypedDict):
	messages: Annotated[list, add_messages]
	user_objective: str  # mandatory input
	files: list[str]  # mandatory input
	plan: list[dict]  # [{"agent": str, "files": list[str], "instructions": str}]
	current_step: int  # index into plan
	step_results: list[dict]  # [{"step": int, "agent": str, "output": str}]
	retries: int  # retry count for current step
	status: str  # planning | executing | done | failed


MAX_RETRIES = 2

# ── LLM factory ──────────────────────────────────────────────────────────────


def _llm():
	return init_chat_model(
		model=os.getenv("LM_MODEL", "claude-haiku-4-5-20251001"),
		temperature=0,
	)


def _build_success_message(total_steps: int, last_output: str) -> str:
	lines = [line.rstrip() for line in last_output.splitlines()]
	try:
		output_idx = lines.index("Output files:")
	except ValueError:
		return f"All {total_steps} step(s) completed successfully."

	paths = [line.strip() for line in lines[output_idx + 1 :] if line.strip()]
	if not paths:
		return f"All {total_steps} step(s) completed successfully."

	return f"All {total_steps} step(s) completed successfully.\n\nOutput files:\n" + "\n".join(
		paths
	)


def _log_node_output(log, payload: dict) -> None:
	preview = json.dumps(payload, default=str)[:2000]
	log.info("node_output", payload=preview)


def _render_run_result(result: dict) -> str:
	status = result.get("status")
	step_results = result.get("step_results", [])

	if step_results:
		last_output = str(step_results[-1].get("output", ""))
		if status == "done":
			return f"Execution completed successfully.\n\nLast step output:\n{last_output}"
		return last_output

	messages = result.get("messages", [])
	if messages:
		last = messages[-1]
		return str(getattr(last, "content", last))

	return "No response available."


# ── Nodes (async) ─────────────────────────────────────────────────────────────


async def planner_node(state: OrchestratorState) -> dict:
	"""Reads objective + files, produces a structured execution plan."""
	log = logger.bind(node="planner")
	log.info("planning", objective=state["user_objective"], files=state["files"])

	llm = _llm().with_structured_output(ExecutionPlan)
	response: ExecutionPlan = await llm.ainvoke(  # type: ignore[assignment]
		[
			SystemMessage(content=PLANNER_SYSTEM),
			HumanMessage(
				content=(
					f"Objective: {state['user_objective']}\nFiles: {', '.join(state['files'])}"
				)
			),
		]
	)

	plan = [response.step.model_dump()]
	log.info("plan_created", steps=1, reasoning=response.reasoning)

	step = plan[0]
	plan_summary = f"  Step 1: {step['agent']} <- {', '.join(step['files'])}"

	result = {
		"plan": plan,
		"current_step": 0,
		"step_results": [],
		"retries": 0,
		"status": "executing",
		"messages": [AIMessage(content=f"Plan ({len(plan)} step(s)):\n{plan_summary}")],
	}
	_log_node_output(log, result)
	return result


async def executor_node(state: OrchestratorState) -> dict:
	"""Executes the current plan step by invoking the assigned sub-agent."""
	log = logger.bind(node="executor")
	step_idx = state["current_step"]
	step = state["plan"][step_idx]
	retries = state.get("retries", 0)
	step_files = step.get("files", [])

	log.info("executing_step", step=step_idx + 1, agent=step["agent"], attempt=retries + 1)

	if step["agent"] == "codegen_agent":
		if len(step_files) != 1:
			output = (
				"FAILURE: codegen_agent requires exactly one input file in the plan step. "
				f"Received {len(step_files)} files."
			)
			log.error("invalid_codegen_step_files", files=step_files)
		else:
			agent_input = {"file_path": step_files[0], "instructions": step["instructions"]}
			output = await invoke_codegen_agent.ainvoke(agent_input)
	elif step["agent"] == "sop_agent":
		agent_input = {"files": step_files, "instructions": step["instructions"]}
		output = await invoke_sop_agent.ainvoke(agent_input)
	else:
		unknown = step["agent"]
		log.error("unknown_agent", agent=unknown)
		output = f"Error: unknown agent '{unknown}'. Supported agents: codegen_agent, sop_agent."

	log.info("step_output", step=step_idx + 1, preview=str(output)[:200])

	# Build updated step_results — replace last entry on retry, append otherwise
	step_results = list(state.get("step_results", []))
	entry = {"step": step_idx, "agent": step["agent"], "output": str(output)}
	if retries > 0 and step_results and step_results[-1]["step"] == step_idx:
		step_results[-1] = entry
	else:
		step_results.append(entry)

	result = {
		"step_results": step_results,
		"messages": [
			AIMessage(content=f"Step {step_idx + 1} ({step['agent']}) output:\n{str(output)[:400]}")
		],
	}
	_log_node_output(log, result)
	return result


async def evaluator_node(state: OrchestratorState) -> dict:
	"""Inspects the last step result and decides: next step, retry, done, or failed."""
	log = logger.bind(node="evaluator")
	current_step = state["current_step"]
	plan = state["plan"]
	retries = state.get("retries", 0)
	total_steps = len(plan)
	last_result = state["step_results"][-1]

	llm = _llm().with_structured_output(StepEvaluation)
	evaluation: StepEvaluation = await llm.ainvoke(  # type: ignore[assignment]
		[
			SystemMessage(content=EVALUATOR_SYSTEM),
			HumanMessage(
				content=(
					f"Step {current_step + 1} of {total_steps}\n"
					f"Agent: {last_result['agent']}\n"
					f"Output:\n{last_result['output']}"
				)
			),
		]
	)

	log.info(
		"evaluation",
		step=current_step + 1,
		success=evaluation.success,
		retry=evaluation.retry,
		reasoning=evaluation.reasoning,
	)

	if evaluation.success:
		if current_step + 1 >= total_steps:
			log.info("orchestration_complete", total_steps=total_steps)
			await log_run.ainvoke(
				{
					"files": ", ".join(state["files"]),
					"intent": state["user_objective"],
					"agent_used": ", ".join(s["agent"] for s in plan),
					"outcome": "success",
				}
			)
			result = {
				"status": "done",
				"messages": [
					AIMessage(
						content=_build_success_message(total_steps, str(last_result["output"]))
					)
				],
			}
			_log_node_output(log, result)
			return result

	# Step failed
	if retries < MAX_RETRIES and evaluation.retry:
		log.warning("retrying_step", step=current_step + 1, attempt=retries + 1)
		result = {
			"retries": retries + 1,
			"messages": [
				AIMessage(
					content=f"Step {current_step + 1} failed (attempt {retries + 1}), retrying..."
				)
			],
		}
		_log_node_output(log, result)
		return result

	log.error("step_failed", step=current_step + 1, reason=evaluation.reasoning)
	await log_run.ainvoke(
		{
			"files": ", ".join(state["files"]),
			"intent": state["user_objective"],
			"agent_used": plan[current_step]["agent"],
			"outcome": "failure",
		}
	)
	result = {
		"status": "failed",
		"messages": [
			AIMessage(
				content=(
					f"Step {current_step + 1} failed after {retries + 1} attempt(s).\n"
					f"Reason: {evaluation.reasoning}"
				)
			)
		],
	}
	_log_node_output(log, result)
	return result


def route_after_evaluator(state: OrchestratorState) -> str:
	if state["status"] in ("done", "failed"):
		return END
	return "executor"


# ── Graph ─────────────────────────────────────────────────────────────────────


def build_agent(
	*,
	draw_graph_png: bool = True,
	graph_png_path: str = "orchestrator_graph.png",
):
	builder = StateGraph(OrchestratorState)

	builder.add_node("planner", planner_node)
	builder.add_node("executor", executor_node)
	builder.add_node("evaluator", evaluator_node)

	builder.add_edge(START, "planner")
	builder.add_edge("planner", "executor")
	builder.add_edge("executor", "evaluator")
	builder.add_conditional_edges(
		"evaluator", route_after_evaluator, {"executor": "executor", END: END}
	)

	agent = builder.compile(checkpointer=MemorySaver())
	if draw_graph_png:
		png_bytes = agent.get_graph().draw_mermaid_png()
		Path(graph_png_path).write_bytes(png_bytes)
	return agent


# ── CLI ───────────────────────────────────────────────────────────────────────


async def _run():
	import argparse

	parser = argparse.ArgumentParser(description="Orchestrator Agent")
	parser.add_argument("objective", help="What the user wants to achieve")
	parser.add_argument("files", nargs="+", help="One or more file paths to process")
	args = parser.parse_args()

	objective = args.objective
	files = [f for f in args.files if f and f.strip()]

	if not files:
		parser.error("At least one file must be provided.")

	session_id = str(uuid.uuid4())
	config = {"configurable": {"thread_id": session_id}}
	log = logger.bind(session_id=session_id)
	log.info("session_start", objective=objective, files=files)

	print("=" * 50)
	print("Orchestrator Agent")
	print(f"Objective : {objective}")
	print(f"Files     : {', '.join(files)}")
	print("=" * 50)

	agent = build_agent()

	# Kick off — planner → executor(s) → evaluator(s), all in one ainvoke
	result = await agent.ainvoke(
		{
			"messages": [HumanMessage(content="Please start.")],
			"user_objective": objective,
			"files": files,
			"plan": [],
			"current_step": 0,
			"step_results": [],
			"retries": 0,
			"status": "planning",
		},
		config=config,
	)
	print(f"\n[Agent]: {_render_run_result(result)}")
	if result.get("status") in ("done", "failed"):
		return

	# Follow-up loop — routes to chat_node
	turn = 0
	while True:
		try:
			user_input = (await asyncio.to_thread(input, "\nYou: ")).strip()
		except (EOFError, KeyboardInterrupt):
			log.info("session_end", turns=turn, reason="interrupt")
			print("\nGoodbye.")
			break

		if user_input.lower() in ("quit", "exit", "q"):
			log.info("session_end", turns=turn, reason="user_quit")
			print("Goodbye.")
			break

		if not user_input:
			continue

		turn += 1
		log.info("user_turn", turn=turn, input_preview=user_input[:100])

		result = await agent.ainvoke(
			{"messages": [HumanMessage(content=user_input)]},
			config=config,
		)
		last = result["messages"][-1]
		log.info("agent_turn", turn=turn, response_preview=str(last.content)[:200])
		print(f"\n[Agent]: {last.content}")


def main():
	asyncio.run(_run())


if __name__ == "__main__":
	main()
