from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DatasetSpec(StrictModel):
    name: str | None = None
    objective: str = "Prepare for analysis"
    expected_grain: str | None = None
    primary_key: list[str] = Field(default_factory=list)


class ColumnContract(StrictModel):
    type: Literal["string", "integer", "decimal", "number", "boolean", "date", "datetime"] | None = None
    nullable: bool = True
    unique: bool = False
    minimum: float | None = None
    maximum: float | None = None
    allowed_values: list[Any] | None = None
    accepted_formats: list[str] = Field(default_factory=list)


class SchemaContract(StrictModel):
    strict: bool = False
    columns: dict[str, ColumnContract] = Field(default_factory=dict)


class QualityRule(StrictModel):
    id: str
    type: Literal["not_null", "unique", "range", "allowed_values", "date_parse", "row_count"]
    columns: list[str] = Field(default_factory=list)
    minimum: float | None = None
    maximum: float | None = None
    values: list[Any] | None = None
    severity: Literal["info", "warning", "error", "critical"] = "error"


class PolicySpec(StrictModel):
    preserve_source: bool = True
    allow_row_deletion: bool = False
    allow_column_deletion: bool = False
    allow_imputation: bool = False
    allow_sentinel_replacement: bool = False
    allow_category_mapping: bool = False
    require_approval_for: list[str] = Field(default_factory=lambda: ["row_deletion", "imputation", "placeholder_replacement", "ambiguous_date_parsing", "lossy_type_conversion"])


class ExecutionSpec(StrictModel):
    max_agent_steps: int = Field(default=20, ge=1, le=100)
    max_operations: int = Field(default=50, ge=1, le=250)
    fail_on_critical_validation: bool = True
    require_user_final_approval: bool = True


class OperationSpec(StrictModel):
    type: Literal[
        "trim_whitespace", "normalize_whitespace", "normalize_case", "parse_numeric",
        "parse_date", "rename_columns", "normalize_column_names", "drop_duplicates",
        "drop_empty_rows", "drop_missing_rows", "drop_empty_columns", "fill_missing", "normalize_categories",
        "replace_sentinels", "cast_type", "validate_constraint"
    ]
    columns: list[str] = Field(default_factory=list)
    arguments: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_arguments(self):
        allowed = {
            "trim_whitespace": set(), "normalize_whitespace": set(),
            "normalize_case": {"mode"}, "parse_numeric": {"decimal", "thousands"},
            "parse_date": {"dayfirst", "format"}, "rename_columns": {"mapping"},
            "normalize_column_names": set(), "drop_duplicates": {"subset", "keep"},
            "drop_empty_rows": set(), "drop_missing_rows": {"sentinels"}, "drop_empty_columns": set(),
            "fill_missing": {"value", "method", "sentinels"}, "normalize_categories": {"mapping"},
            "replace_sentinels": {"values"}, "cast_type": {"target"},
            "validate_constraint": {"rule"},
        }
        unknown = set(self.arguments) - allowed[self.type]
        if unknown:
            raise ValueError(f"Unknown arguments for {self.type}: {sorted(unknown)}")
        required = {"normalize_categories": {"mapping"},
                    "replace_sentinels": {"values"}, "cast_type": {"target"},
                    "rename_columns": {"mapping"}}
        missing = required.get(self.type, set()) - set(self.arguments)
        if missing:
            raise ValueError(f"Missing required arguments for {self.type}: {sorted(missing)}")
        if self.type == "fill_missing":
            if ("value" in self.arguments) == ("method" in self.arguments):
                raise ValueError("fill_missing requires exactly one of value or method")
            if self.arguments.get("method") not in {None, "mean", "median", "mode"}:
                raise ValueError("fill_missing method must be mean, median, or mode")
        if "sentinels" in self.arguments and (not isinstance(self.arguments["sentinels"], list)
                or not all(isinstance(value, str) for value in self.arguments["sentinels"])):
            raise ValueError("sentinels must be a list of strings")
        if self.type == "replace_sentinels" and (not self.arguments.get("values")
                or not all(isinstance(value, str) for value in self.arguments["values"])):
            raise ValueError("replace_sentinels requires a non-empty list of text values")
        if self.type == "parse_numeric":
            decimal = self.arguments.get("decimal", ".")
            thousands = self.arguments.get("thousands", ",")
            if decimal not in {".", ","} or thousands not in {".", ",", " "} or decimal == thousands:
                raise ValueError("parse_numeric decimal/thousands separators are invalid")
        if self.type == "parse_date" and "dayfirst" in self.arguments and not isinstance(self.arguments["dayfirst"], bool):
            raise ValueError("parse_date dayfirst must be boolean")
        if self.type == "normalize_case" and self.arguments.get("mode", "lower") not in {"lower", "upper", "title"}:
            raise ValueError("normalize_case mode must be lower, upper, or title")
        if self.type == "drop_duplicates" and self.arguments.get("keep", "first") not in {"first", "last"}:
            raise ValueError("drop_duplicates keep must be first or last")
        return self


class PreparationSpec(StrictModel):
    version: str = "1.0"
    dataset: DatasetSpec = Field(default_factory=DatasetSpec)
    schema_: SchemaContract = Field(default_factory=SchemaContract, alias="schema")
    quality_rules: list[QualityRule] = Field(default_factory=list)
    policies: PolicySpec = Field(default_factory=PolicySpec)
    execution: ExecutionSpec = Field(default_factory=ExecutionSpec)
    operations: list[OperationSpec] = Field(default_factory=list)
    # A concise legacy field keeps the initial form's example readable; both are explicit.
    objective: str | None = None

    @model_validator(mode="after")
    def reconcile_objective(self):
        if self.objective:
            self.dataset.objective = self.objective
        else:
            self.objective = self.dataset.objective
        return self


def parse_yaml_spec(text: str) -> PreparationSpec:
    import yaml
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Preparation YAML must contain a mapping.")
    return PreparationSpec.model_validate(raw)
