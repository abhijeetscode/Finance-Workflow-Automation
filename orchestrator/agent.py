import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Annotated

import structlog
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from orchestrator.memory import find_similar_runs, format_runs_for_context
from orchestrator.tools import ALL_TOOLS

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

# ── Prompt ────────────────────────────────────────────────────────────────────

BASE_SYSTEM_PROMPT = """You are an Orchestration Agent for automating financial workflows.

## Role
Your sole responsibility is routing. You read the user's objective, decide which sub-agent can fulfil it, and delegate — passing the right files and instructions. You do not process files yourself.

## Sub-agents
- **codegen_agent**: transforms Excel files into a fixed audit output (4-sheet Excel + CSV).
- **sop_agent**: executes a business process by following steps in an SOP document.

## How to route
1. Read user_objective from the session context — this is your only input for the routing decision.
2. Match the objective to a sub-agent:
   - transform / process / audit / convert / payments → codegen_agent
   - follow SOP / execute steps / run process → sop_agent
3. If the objective is clear, invoke the sub-agent immediately with the provided files.
4. If genuinely ambiguous, ask ONE question using ask_user — then invoke.
5. After the sub-agent finishes, report the exact output file paths to the user.
6. Call log_run to record this run in memory.

## Rules
- Route from the objective alone — do not read or inspect files to make the routing decision.
- You are a router, not a processor. Never handle file content yourself.
- Never ask the user to repeat their objective.
- Be decisive. One routing decision, then delegate.
"""

# ── State ─────────────────────────────────────────────────────────────────────


class OrchestratorState(TypedDict):
	messages: Annotated[list, add_messages]
	user_objective: str  # mandatory — what the user wants to achieve
	files: list[str]  # mandatory — file paths provided by the user
	system_prompt: str  # built at session start, includes memory context


# ── Tool map ──────────────────────────────────────────────────────────────────

_TOOL_MAP = {t.name: t for t in ALL_TOOLS}


# ── Node ──────────────────────────────────────────────────────────────────────


def orchestrator_node(state: OrchestratorState) -> dict:
	"""Single node: resolves objective + files from state, calls LLM, executes tools, loops until done."""
	log = logger.bind(node="orchestrator")

	llm = init_chat_model(
		model=os.getenv("LM_MODEL", "claude-haiku-4-5-20251001"),
		temperature=0,
	)
	llm_with_tools = llm.bind_tools(ALL_TOOLS)

	# Inject objective + files into the system prompt so the LLM has full context
	objective = state.get("user_objective", "")
	files = state.get("files", [])
	system_prompt = state.get("system_prompt", BASE_SYSTEM_PROMPT)
	if objective or files:
		system_prompt += (
			f"\n\n## Session Context\nUser objective: {objective}\nFiles: {', '.join(files)}"
		)

	messages = [SystemMessage(content=system_prompt)] + list(state["messages"])
	start_len = len(messages)

	log.debug("node_entry", objective=objective, files=files, message_count=len(state["messages"]))

	llm_call = 0
	while True:
		llm_call += 1
		log.debug("llm_invoke", attempt=llm_call, context_messages=len(messages))

		response = llm_with_tools.invoke(messages)
		messages.append(response)

		if not response.tool_calls:
			log.debug("llm_final_response", content_preview=str(response.content)[:200])
			break

		log.debug("tool_calls_received", count=len(response.tool_calls))

		for tc in response.tool_calls:
			log.info(
				"tool_call",
				tool=tc["name"],
				args={k: str(v)[:100] for k, v in tc["args"].items()},
			)
			tool_fn = _TOOL_MAP[tc["name"]]
			result = tool_fn.invoke(tc["args"])
			log.debug("tool_result", tool=tc["name"], result_preview=str(result)[:200])
			messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

	new_messages = messages[start_len:]
	log.debug("node_exit", new_messages=len(new_messages), total_llm_calls=llm_call)

	return {"messages": new_messages}


# ── Graph ─────────────────────────────────────────────────────────────────────


def build_agent():
	builder = StateGraph(OrchestratorState)
	builder.add_node("orchestrator", orchestrator_node)
	builder.set_entry_point("orchestrator")
	builder.add_edge("orchestrator", END)
	return builder.compile(checkpointer=MemorySaver())


# ── Helpers ───────────────────────────────────────────────────────────────────


def build_system_prompt(files: list[str]) -> str:
	"""Extend the base prompt with past run history for similar files."""
	similar_runs = find_similar_runs(files)
	if not similar_runs:
		logger.debug("memory_lookup", files=files, matches=0)
		return BASE_SYSTEM_PROMPT
	logger.info("memory_lookup", files=files, matches=len(similar_runs))
	history = format_runs_for_context(similar_runs)
	return (
		BASE_SYSTEM_PROMPT
		+ "\n\n## Past runs for similar files\n"
		+ history
		+ "\n\nUse this history to confirm intent without re-asking."
	)


# ── CLI ───────────────────────────────────────────────────────────────────────


def main():
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
	print("Type 'quit' to exit")
	print("=" * 50)

	agent = build_agent()
	system_prompt = build_system_prompt(files)

	# Kick off the agent with objective + files already in state
	result = agent.invoke(
		{
			"messages": [HumanMessage(content="Please start.")],
			"user_objective": objective,
			"files": files,
			"system_prompt": system_prompt,
		},
		config=config,
	)
	print(f"\n[Agent]: {result['messages'][-1].content}")

	turn = 0
	while True:
		try:
			user_input = input("\nYou: ").strip()
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

		result = agent.invoke(
			{"messages": [HumanMessage(content=user_input)]},
			config=config,
		)
		last = result["messages"][-1]
		log.info("agent_turn", turn=turn, response_preview=str(last.content)[:200])
		print(f"\n[Agent]: {last.content}")


if __name__ == "__main__":
	main()
