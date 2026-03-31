import os
import sys
from pathlib import Path

from orchestrator.agent import main


SCENARIOS = {
	"sop": [
		"run_orchestrator.py",
		"use the SOP and supporting files to prepare the fixed asset rollforward output",
		"./raw_data/Fixed Asset Rollforward Instructions.docx",
		"./raw_data/Outpu_Template.xlsx",
		"./raw_data/BalanceSheet-544.xls",
		"./raw_data/TrialBalance171.xls",
		"./raw_data/FAMAdditionsResults912.xls",
		"./raw_data/FAMDisposalsResults708.xls",
		"./raw_data/FAM_DeprSchedule_FPR72454.xml",
		"./raw_data/FAM_SummaryReport_FPR72456.xml",
	],
	"vendor_payments": [
		"run_orchestrator.py",
		"transform vendor payments into standard audit format",
		"./inputs/[Simple] Bill processing_tampered_2.xlsx",
	],
}


if __name__ == "__main__":
	scenario = "sop"

	os.chdir(Path(__file__).resolve().parents[1])
	sys.argv = SCENARIOS[scenario]
	main()
