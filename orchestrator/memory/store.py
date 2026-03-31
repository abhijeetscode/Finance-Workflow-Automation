import json
from datetime import datetime
from pathlib import Path

import structlog

logger = structlog.get_logger("Orchestrator.Memory")

MEMORY_DIR = Path("./memory")
RUNS_FILE = MEMORY_DIR / "orchestrator_runs.json"


def load_runs() -> list[dict]:
	if not RUNS_FILE.exists():
		return []
	return json.loads(RUNS_FILE.read_text())


def save_run(files: list[str], intent: str, agent: str, outcome: str) -> None:
	from .fingerprint import fingerprint_file
	MEMORY_DIR.mkdir(exist_ok=True)
	runs = load_runs()
	entry = {
		"timestamp": datetime.now().isoformat(),
		"files": [{"path": f, "fingerprint": fingerprint_file(f)} for f in files],
		"intent": intent,
		"agent": agent,
		"outcome": outcome,
	}
	runs.append(entry)
	RUNS_FILE.write_text(json.dumps(runs, indent=2))
	logger.info("run_saved", intent=intent, agent=agent, outcome=outcome, file_count=len(files))


def find_similar_runs(file_paths: list[str]) -> list[dict]:
	"""Return the last 5 runs that share a file fingerprint with any of the given files."""
	from .fingerprint import fingerprint_file
	runs = load_runs()
	if not runs:
		logger.debug("memory_empty")
		return []
	fingerprints = {fingerprint_file(f) for f in file_paths if Path(f).exists()}
	logger.debug("fingerprints_computed", fingerprints=list(fingerprints))
	matches = [
		run for run in runs
		if fingerprints & {f["fingerprint"] for f in run.get("files", [])}
	]
	logger.debug("similar_runs_found", total_runs=len(runs), matches=len(matches))
	return matches[-5:]


def format_runs_for_context(runs: list[dict]) -> str:
	lines = []
	for r in runs:
		files = ", ".join(f["path"] for f in r["files"])
		lines.append(
			f"- [{r['timestamp'][:10]}] files={files} | intent={r['intent']} "
			f"| agent={r['agent']} | outcome={r['outcome']}"
		)
	return "\n".join(lines)
