"""All human-in-the-loop interactions."""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def ask_human(question: str, context: str = "") -> str:
    """Print question + optional context, block for human input."""
    print()
    if context:
        print(f"  Context: {context}")
    print(f"  > {question}")
    response = input("  Your answer: ").strip()
    logger.debug("ask_human | Q: %s | A: %s", question, response)
    return response


def gather_file_context(file_paths: list[Path]) -> dict[str, str]:
    """Ask human what each input file represents before parsing the SOP."""
    print("\n" + "=" * 60)
    print("FILE CONTEXT — please describe each input file")
    print("This helps the agent interpret the SOP correctly.")
    print("=" * 60)

    context: dict[str, str] = {}
    for path in file_paths:
        answer = ask_human(f"What does '{path.name}' represent in this workflow?")
        context[path.name] = answer
        logger.info("File context: %s → %s", path.name, answer)

    return context


def verify_before_write(step_number: int, preview: str) -> tuple[str, str | None]:
    """Checkpoint 1 — show human what is about to be written, get approval."""
    print(f"\n[Step {step_number}] About to write:")
    print(preview)
    answer = ask_human("Proceed? (yes/no)").lower()
    if answer == "yes":
        logger.info("Step %d write approved.", step_number)
        return "ok", None
    feedback = ask_human("What should be corrected?")
    logger.info("Step %d write rejected. Feedback: %s", step_number, feedback)
    return "retry", feedback


def verify_step_completion(step_number: int, result_summary: str) -> tuple[str, str | None]:
    """Checkpoint 2 — after step is done, ask human if output is correct."""
    print(f"\n[Step {step_number}] Completed:")
    print(result_summary)
    answer = ask_human("Does the output look correct? (yes/no)").lower()
    if answer == "yes":
        logger.info("Step %d accepted by human.", step_number)
        return "ok", None
    feedback = ask_human("What was wrong?")
    logger.info("Step %d rejected. Feedback: %s", step_number, feedback)
    return "retry", feedback
