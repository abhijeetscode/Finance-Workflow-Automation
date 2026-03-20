from pydantic import BaseModel


# ── Learn Phase Models ───────────────────────────────────────────────────────


class InputColumnSignature(BaseModel):
    semantic_name: str
    description: str
    data_type: str  # "string", "date", "currency", "integer"
    example_values: list[str]
    identifying_patterns: list[str]
    critical: bool


class InputSheetSignature(BaseModel):
    semantic_role: str
    description: str
    columns: list[InputColumnSignature]
    identifying_traits: list[str]
    critical: bool


class OutputColumnSpec(BaseModel):
    name: str
    source_semantic: str  # "none" if derived
    transformation: str  # "passthrough", "format_date", "lookup", "derived"
    description: str


class OutputSheetSpec(BaseModel):
    name: str
    purpose: str
    columns: list[OutputColumnSpec]
    filter_rule: str


class TransformationRule(BaseModel):
    name: str
    description: str
    input_roles: list[str]
    output_sheet: str
    logic: str


class SemanticValidation(BaseModel):
    name: str
    description: str
    check_logic: str


class BusinessLogicSpec(BaseModel):
    input_sheets: list[InputSheetSignature]
    output_sheets: list[OutputSheetSpec]
    transformation_rules: list[TransformationRule]
    semantic_validations: list[SemanticValidation]
    output_file_format: str


# ── Adapt Phase Models ───────────────────────────────────────────────────────


class SheetMatch(BaseModel):
    semantic_role: str
    actual_sheet_name: str | None
    confidence: float
    confidence_breakdown: dict[str, float]
    critical: bool


class ColumnMatch(BaseModel):
    semantic_name: str
    actual_column_name: str | None
    sheet_role: str
    confidence: float
    confidence_breakdown: dict[str, float]
    critical: bool


class InputMapping(BaseModel):
    sheet_matches: list[SheetMatch]
    column_matches: list[ColumnMatch]
    missing_fields: list[str]
    feasibility: str  # "proceed", "ask_human", "terminate"
    overall_confidence: float
