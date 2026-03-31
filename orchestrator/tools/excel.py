from pathlib import Path

import pandas as pd
from langchain_core.tools import tool
from tabulate import tabulate


@tool
def list_sheets(file_path: str) -> str:
	"""List all sheet names in an Excel file with row and column counts. Call this first to get an overview before deciding which sheets to read."""
	try:
		xl = pd.ExcelFile(file_path)
		lines = []
		for name in xl.sheet_names:
			df = pd.read_excel(xl, sheet_name=name)
			lines.append(f"  - {name}: {len(df)} rows × {len(df.columns)} cols")
		return f"Sheets in '{Path(file_path).name}':\n" + "\n".join(lines)
	except Exception as e:
		return f"Error reading '{file_path}': {e}"


@tool
def read_excel_sheet(file_path: str, sheet_name: str, nrows: int = 5) -> str:
	"""Read a sample of a specific sheet from an Excel file. Only call for sheets relevant to the user's objective."""
	try:
		df = pd.read_excel(file_path, sheet_name=sheet_name, nrows=nrows)
		sample = tabulate(df, headers="keys", tablefmt="pipe", showindex=False)
		return f"Sheet '{sheet_name}' in '{Path(file_path).name}' — first {nrows} rows:\n{sample}"
	except Exception as e:
		return f"Error reading sheet '{sheet_name}': {e}"
