"""Parses a SOP document into ordered executable steps using DSPy."""

import logging
from docx import Document

import dspy

from signatures import ParseSOPToSteps, SOPStep

logger = logging.getLogger(__name__)


def read_docx(path: str) -> str:
    doc = Document(path)
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


class SOPParser(dspy.Module):
    def __init__(self):
        self.parse = dspy.ChainOfThought(ParseSOPToSteps)

    def forward(self, sop_text: str, file_context: str) -> list[SOPStep]:
        logger.debug("SOPParser: parsing SOP (%d chars)", len(sop_text))
        result = self.parse(sop_text=sop_text, file_context=file_context)
        logger.info("SOPParser: %d steps extracted", len(result.steps))
        for step in result.steps:
            logger.debug("  Step %d: %s", step.step_number, step.description)
        return result.steps
