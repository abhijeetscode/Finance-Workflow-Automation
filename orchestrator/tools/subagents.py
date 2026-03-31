from langchain_core.tools import tool


@tool
def log_run(files: str, intent: str, agent_used: str, outcome: str) -> str:
	"""Save a completed run to long-term memory. Call this after a sub-agent finishes.
	Args:
	    files: comma-separated file paths that were processed
	    intent: what the user wanted to do (one sentence)
	    agent_used: which sub-agent was invoked (codegen_agent or sop_agent)
	    outcome: 'success' or 'failure'
	"""
	from orchestrator.memory.store import save_run

	file_list = [f.strip() for f in files.split(",") if f.strip()]
	save_run(files=file_list, intent=intent, agent=agent_used, outcome=outcome)
	return "Run saved to memory."


@tool
def ask_user(question: str) -> str:
	"""Ask the user a clarifying question and return their answer."""
	print(f"\n[Agent]: {question}")
	return input("You: ").strip()


@tool
def invoke_codegen_agent(file_path: str, instructions: str) -> str:
	"""Delegate to the code generation agent to transform an Excel file into standard output format."""
	try:
		from codegen_agent import CodeGenAgent

		agent = CodeGenAgent()
		result = agent.run(input_file_path=file_path)
		files = result.get("output_files", [])

		# Primary signal: new files detected by directory snapshot
		if files:
			paths = "\n".join(f"  - {f}" for f in files)
			return f"SUCCESS: Transformation complete. Output files:\n{paths}"

		return "FAILURE: CodeGen agent completed without producing output files. The agent already attempted internal retries — do not retry."
	except Exception as e:
		return f"FAILURE: CodeGen agent raised an exception: {e}"


@tool
def invoke_sop_agent(file_path: str, instructions: str) -> str:
	"""Delegate to the SOP agent to execute steps from an SOP document."""
	from orchestrator.sop_agent import run

	result = run(file_path=file_path, instructions=instructions)
	status = "SUCCESS" if result.get("status") != "failure" else "FAILURE"
	return f"{status}: {result['message']}"
