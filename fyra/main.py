"""
SOP Automation Agent — Entry Point

Usage:
    python main.py
    python main.py --sop ../raw_data/my_sop.docx --output ../raw_data/output.xlsx
"""

import argparse
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv("../.env")  # Load environment variables from .env file


# ---------------------------------------------------------------------------
# Paths — all raw_data files relative to project root
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent
RAW_DATA = PROJECT_ROOT / "raw_data" / "zalos_test_data"

DEFAULT_SOP = RAW_DATA / "Avalara VAT Report SOP.docx"

# The file agent will write into (a copy is made — original never touched)
DEFAULT_OUTPUT = RAW_DATA / "VAT-Avalara Sales Tax December 2025 ZalosCopy.xlsx"

# All input/reference files the agent can read
DEFAULT_INPUT_FILES = [
	RAW_DATA / "SalesTaxDocumentLineExport-Dec 2025.xlsx",
]

OUTPUT_DIR = Path(__file__).parent / "output"
LOGS_DIR = Path(__file__).parent / "logs"

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


_NOISY_LOGGERS = [
	"httpcore",
	"httpx",
	"LiteLLM",
	"litellm",
	"openai",
	"anthropic",
	"urllib3",
	"dspy.utils.callback",
	"dspy.clients",
	"dspy.adapters",
]


def setup_logging(run_id: str) -> logging.Logger:
	LOGS_DIR.mkdir(exist_ok=True)

	log_file = LOGS_DIR / f"run_{run_id}.log"

	fmt = logging.Formatter(
		fmt="%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
		datefmt="%Y-%m-%d %H:%M:%S",
	)

	# File handler — DEBUG for our code only
	file_handler = logging.FileHandler(log_file, encoding="utf-8")
	file_handler.setLevel(logging.DEBUG)
	file_handler.setFormatter(fmt)

	# Console handler — INFO and above
	console_handler = logging.StreamHandler(sys.stdout)
	console_handler.setLevel(logging.INFO)
	console_handler.setFormatter(fmt)

	root_logger = logging.getLogger()
	root_logger.setLevel(logging.DEBUG)
	root_logger.addHandler(file_handler)
	root_logger.addHandler(console_handler)

	# Silence noisy third-party loggers in both file and console
	for name in _NOISY_LOGGERS:
		logging.getLogger(name).setLevel(logging.WARNING)

	logger = logging.getLogger("main")
	logger.info("Logging initialised — file: %s", log_file)
	return logger


# ---------------------------------------------------------------------------
# Output file copy
# ---------------------------------------------------------------------------


def copy_output_file(output_path: Path, run_id: str) -> Path:
	OUTPUT_DIR.mkdir(exist_ok=True)
	stem = output_path.stem
	suffix = output_path.suffix
	dest = OUTPUT_DIR / f"{stem}_{run_id}{suffix}"
	shutil.copy2(output_path, dest)
	return dest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="SOP Automation Agent — executes a financial SOP step by step"
	)
	parser.add_argument(
		"--sop",
		type=Path,
		default=DEFAULT_SOP,
		help="Path to the SOP docx file (default: raw_data/Fixed Asset Rollforward Instructions.docx)",
	)
	parser.add_argument(
		"--output",
		type=Path,
		default=DEFAULT_OUTPUT,
		help="Path to the output xlsx file to work on (a timestamped copy will be made)",
	)
	parser.add_argument(
		"--files",
		type=Path,
		nargs="*",
		default=DEFAULT_INPUT_FILES,
		help="Input/reference files the agent can read (default: all files in raw_data/)",
	)
	return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
	args = parse_args()
	run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
	logger = setup_logging(run_id)

	logger.info("=" * 60)
	logger.info("SOP Automation Agent — run %s", run_id)
	logger.info("=" * 60)

	# Validate paths
	if not args.sop.exists():
		logger.error("SOP file not found: %s", args.sop)
		sys.exit(1)

	if not args.output.exists():
		logger.error("Output file not found: %s", args.output)
		sys.exit(1)

	missing = [f for f in args.files if not f.exists()]
	if missing:
		for f in missing:
			logger.warning("Input file not found, will be skipped: %s", f)
		args.files = [f for f in args.files if f.exists()]

	# Copy output file — original never touched again after this
	working_copy = copy_output_file(args.output, run_id)
	logger.info("Output file copied to: %s", working_copy)
	logger.info("Original output file will NOT be modified: %s", args.output)

	logger.info("SOP file     : %s", args.sop)
	logger.info("Working copy : %s", working_copy)
	logger.info("Input files  :")
	for f in args.files:
		logger.info("  %s", f)

	from agent import run_agent

	run_agent(
		sop_path=args.sop,
		output_path=working_copy,
		input_files=args.files,
		run_id=run_id,
	)

	logger.info("Agent run complete. Output: %s", working_copy)


if __name__ == "__main__":
	main()
