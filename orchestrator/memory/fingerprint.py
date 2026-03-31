import hashlib
from pathlib import Path


def fingerprint_file(file_path: str) -> str:
	"""Generate a structural fingerprint for a file based on its content shape, not its name."""
	path = Path(file_path)
	if not path.exists():
		return "unknown"
	if path.suffix == ".xlsx":
		return _fingerprint_excel(file_path)
	if path.suffix == ".docx":
		return _fingerprint_docx(file_path)
	return hashlib.md5(path.name.encode()).hexdigest()[:8]


def _fingerprint_excel(file_path: str) -> str:
	import pandas as pd
	xl = pd.ExcelFile(file_path)
	parts = []
	for sheet in sorted(xl.sheet_names):
		df = pd.read_excel(xl, sheet_name=sheet, nrows=0)
		cols = ",".join(sorted(df.columns.astype(str).tolist()))
		parts.append(f"{sheet}:{cols}")
	return hashlib.md5("|".join(parts).encode()).hexdigest()[:12]


def _fingerprint_docx(file_path: str) -> str:
	from docx import Document
	doc = Document(file_path)
	headings = sorted(p.text for p in doc.paragraphs if p.style.name.startswith("Heading"))
	return hashlib.md5("|".join(headings).encode()).hexdigest()[:12]
