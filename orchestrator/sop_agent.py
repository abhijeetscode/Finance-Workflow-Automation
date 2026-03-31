import re
from dataclasses import dataclass
from pathlib import Path

from docx import Document


@dataclass
class SOPStep:
	step_number: int
	description: str


def _find_sop_file(files: list[str]) -> Path | None:
	docx_files = [
		Path(file_path) for file_path in files if Path(file_path).suffix.lower() == ".docx"
	]
	if not docx_files:
		return None
	return docx_files[0]


def _supporting_files(files: list[str], sop_path: Path) -> list[Path]:
	return [Path(file_path) for file_path in files if Path(file_path) != sop_path]


def _read_docx(path: Path) -> list[str]:
	doc = Document(path)
	return [para.text.strip() for para in doc.paragraphs if para.text.strip()]


def _looks_like_step(text: str) -> bool:
	if re.match(r"^\d+[\.\)]\s+", text):
		return True
	if re.match(r"^(step|phase)\s+\d+[:\-]?\s+", text, re.IGNORECASE):
		return True
	if text.startswith(("- ", "* ", "• ")):
		return True
	return False


def _normalize_step_text(text: str) -> str:
	text = re.sub(r"^\d+[\.\)]\s+", "", text).strip()
	text = re.sub(r"^(step|phase)\s+\d+[:\-]?\s+", "", text, flags=re.IGNORECASE).strip()
	text = re.sub(r"^[-*•]\s+", "", text).strip()
	return text


def _extract_steps(paragraphs: list[str]) -> list[SOPStep]:
	steps: list[SOPStep] = []
	for paragraph in paragraphs:
		if not _looks_like_step(paragraph):
			continue
		description = _normalize_step_text(paragraph)
		if not description:
			continue
		steps.append(SOPStep(step_number=len(steps) + 1, description=description))

	if steps:
		return steps

	fallback = [paragraph for paragraph in paragraphs if len(paragraph.split()) >= 5]
	return [
		SOPStep(step_number=index + 1, description=paragraph)
		for index, paragraph in enumerate(fallback[:12])
	]


def run(files: list[str], instructions: str) -> dict:
	"""
	Parse an SOP document and produce an execution-ready summary.
	Args:
	    files: paths to the SOP document and any supporting files
	    instructions: additional user instructions
	Returns:
	    result dict with status and message
	"""
	if not files:
		return {"status": "failure", "message": "No files were provided to the SOP agent."}

	sop_path = _find_sop_file(files)
	if sop_path is None:
		return {
			"status": "failure",
			"message": "No SOP document (.docx) was provided to the SOP agent.",
		}

	if not sop_path.exists():
		return {
			"status": "failure",
			"message": f"SOP document not found: {sop_path}",
		}

	try:
		paragraphs = _read_docx(sop_path)
	except Exception as exc:
		return {
			"status": "failure",
			"message": f"Failed to read SOP document '{sop_path.name}': {exc}",
		}

	steps = _extract_steps(paragraphs)
	if not steps:
		return {
			"status": "failure",
			"message": f"No executable steps could be extracted from '{sop_path.name}'.",
		}

	supporting = _supporting_files(files, sop_path)
	step_preview = "\n".join(f"  {step.step_number}. {step.description}" for step in steps[:5])
	if len(steps) > 5:
		step_preview += f"\n  ... and {len(steps) - 5} more step(s)"

	supporting_summary = (
		"\n".join(f"  - {path.name}" for path in supporting) if supporting else "  - none"
	)

	instruction_summary = instructions.strip() if instructions.strip() else "none"

	return {
		"status": "success",
		"message": (
			f"Parsed SOP '{sop_path.name}' into {len(steps)} step(s).\n"
			f"Supporting files:\n{supporting_summary}\n"
			f"Additional instructions: {instruction_summary}\n"
			f"Step preview:\n{step_preview}"
		),
	}
