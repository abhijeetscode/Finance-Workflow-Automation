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


def gather_file_context(
    file_paths: list[Path],
    inferred: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build file context dict.

    If `inferred` is provided (filename → auto-inferred description), shows each
    inference to the user for a quick confirm-or-correct instead of asking from scratch.
    Falls back to asking from scratch for any file not in `inferred`.
    """
    print("\n" + "=" * 60)
    print("FILE CONTEXT — confirm or correct each file description")
    print("=" * 60)

    context: dict[str, str] = {}
    for path in file_paths:
        name = path.name
        if inferred and name in inferred:
            print(f"\n  File: {name}")
            print(f"  Inferred: {inferred[name]}")
            answer = input("  Correct? Press Enter to accept, or type a correction: ").strip()
            description = answer if answer else inferred[name]
        else:
            description = ask_human(f"What does '{name}' represent in this workflow?")
        context[name] = description
        logger.info("File context: %s → %s", name, description)

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
