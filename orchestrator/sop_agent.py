"""Placeholder SOP Agent — replace with real implementation later."""


def run(files: list[str], instructions: str) -> dict:
	"""
	Execute steps from an SOP document.
	Args:
	    files: paths to the SOP document and any supporting files
	    instructions: additional user instructions
	Returns:
	    result dict with status and message
	"""
	print(f"[SOP Agent] Would process files: {files}")
	print(f"[SOP Agent] Instructions: {instructions}")
	return {
		"status": "placeholder",
		"message": f"SOP Agent is not yet implemented. Would have processed {files}.",
	}
