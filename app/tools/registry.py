from __future__ import annotations

import re
from typing import Any

import pandas as pd
from pydantic import ValidationError

from app.specifications.schema import OperationSpec

DESTRUCTIVE_ROW_TOOLS = {"drop_duplicates", "drop_empty_rows"}
DESTRUCTIVE_COLUMN_TOOLS = {"drop_empty_columns"}


def execute_operation(frame: pd.DataFrame, raw_spec: dict[str, Any]) -> pd.DataFrame:
    """Execute one allowlisted operation on a copy; reject unknown args/columns."""
    spec = OperationSpec.model_validate(raw_spec)
    out = frame.copy(deep=True)
    missing = set(spec.columns) - set(out.columns)
    if missing:
        raise ValueError(f"Unknown target columns: {sorted(missing)}")
    args = spec.arguments
    if spec.type == "trim_whitespace":
        for col in spec.columns:
            out[col] = out[col].map(lambda x: x.strip() if isinstance(x, str) else x)
    elif spec.type == "normalize_whitespace":
        for col in spec.columns:
            out[col] = out[col].map(lambda x: re.sub(r"\s+", " ", x).strip() if isinstance(x, str) else x)
    elif spec.type == "normalize_case":
        mode = args.get("mode", "lower")
        if mode not in {"lower", "upper", "title"}:
            raise ValueError("mode must be lower, upper, or title")
        for col in spec.columns:
            fn = {"lower": str.lower, "upper": str.upper, "title": str.title}[mode]
            out[col] = out[col].map(lambda x: fn(x) if isinstance(x, str) else x)
    elif spec.type == "parse_numeric":
        decimal = args.get("decimal", ".")
        thousands = args.get("thousands", ",")
        if decimal not in {".", ","} or thousands not in {".", ",", " "} or decimal == thousands:
            raise ValueError("Unsupported decimal/thousands separators")
        for col in spec.columns:
            text = out[col].astype("string").str.replace(r"[$£€]", "", regex=True).str.strip()
            if thousands != decimal:
                text = text.str.replace(thousands, "", regex=False)
            if decimal != ".":
                text = text.str.replace(decimal, ".", regex=False)
            parsed = pd.to_numeric(text, errors="coerce")
            failed = int((out[col].notna() & parsed.isna()).sum())
            if failed:
                raise ValueError(f"Numeric parse would lose {failed} non-null values in {col!r}")
            out[col] = parsed
    elif spec.type == "parse_date":
        dayfirst = args.get("dayfirst", False)
        if not isinstance(dayfirst, bool):
            raise ValueError("dayfirst must be boolean")
        date_format = args.get("format")
        for col in spec.columns:
            parsed = pd.to_datetime(out[col], errors="coerce", dayfirst=dayfirst, format=date_format or "mixed")
            failed = int((out[col].notna() & parsed.isna()).sum())
            if failed:
                raise ValueError(f"Date parse would lose {failed} non-null values in {col!r}")
            out[col] = parsed
    elif spec.type == "rename_columns":
        mapping = args.get("mapping")
        if not isinstance(mapping, dict) or set(mapping) - set(out.columns):
            raise ValueError("mapping must map existing column names")
        targets = list(out.rename(columns=mapping).columns)
        if len(set(targets)) != len(targets):
            raise ValueError("Rename would create duplicate headers")
        out = out.rename(columns=mapping)
    elif spec.type == "normalize_column_names":
        mapping = {}
        for col in out.columns:
            if col == "_source_row_id":
                continue
            normalized = re.sub(r"[^0-9a-zA-Z]+", "_", str(col).strip()).strip("_").lower()
            if normalized and normalized[0].isdigit():
                normalized = f"col_{normalized}"
            mapping[col] = normalized or "column"
        if len(set(mapping.values())) != len(mapping):
            raise ValueError("Column-name normalization would create collisions")
        out = out.rename(columns=mapping)
    elif spec.type == "drop_duplicates":
        subset = args.get("subset")
        if subset is None:
            subset = [column for column in out.columns if column != "_source_row_id"]
        if subset is not None and (not isinstance(subset, list) or set(subset) - set(out.columns)):
            raise ValueError("subset must list existing columns")
        if "_source_row_id" in (subset or []):
            raise ValueError("Source identity cannot be used as a duplicate key")
        keep = args.get("keep", "first")
        if keep not in {"first", "last"}:
            raise ValueError("keep must be first or last")
        out = out.drop_duplicates(subset=subset, keep=keep)
    elif spec.type == "drop_empty_rows":
        subset = spec.columns or [column for column in out.columns if column != "_source_row_id"]
        out = out.dropna(axis=0, how="all", subset=subset)
    elif spec.type == "drop_empty_columns":
        drop = [c for c in (spec.columns or list(out.columns)) if c != "_source_row_id" and out[c].isna().all()]
        out = out.drop(columns=drop)
    elif spec.type == "fill_missing":
        if "value" not in args:
            raise ValueError("fill_missing requires an explicit value")
        out[spec.columns] = out[spec.columns].fillna(args["value"])
    elif spec.type == "replace_sentinels":
        sentinels = args.get("values")
        if not isinstance(sentinels, list) or not sentinels:
            raise ValueError("replace_sentinels requires an explicit non-empty values list")
        for col in spec.columns:
            out[col] = out[col].replace(sentinels, pd.NA)
    elif spec.type == "normalize_categories":
        mapping = args.get("mapping")
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError("normalize_categories requires an explicit mapping")
        for col in spec.columns:
            out[col] = out[col].map(lambda x: mapping.get(x, x))
    elif spec.type == "cast_type":
        target = args.get("target")
        if target not in {"string", "integer", "decimal", "number", "boolean", "date", "datetime"}:
            raise ValueError("Unsupported target type")
        for col in spec.columns:
            source = out[col]
            if target == "string":
                out[col] = source.astype("string")
            elif target in {"decimal", "number", "integer"}:
                parsed = pd.to_numeric(source, errors="coerce")
                if (source.notna() & parsed.isna()).any():
                    raise ValueError(f"Cast to numeric would lose values in {col!r}")
                if target == "integer":
                    if ((parsed.dropna() % 1) != 0).any():
                        raise ValueError(f"Cast to integer would lose fractional values in {col!r}")
                    parsed = parsed.astype("Int64")
                out[col] = parsed
            elif target in {"date", "datetime"}:
                parsed = pd.to_datetime(source, errors="coerce", format="mixed")
                if (source.notna() & parsed.isna()).any():
                    raise ValueError(f"Cast to date would lose values in {col!r}")
                out[col] = parsed
            else:
                parsed = source.astype("string").str.lower().map({"true": True, "false": False, "yes": True, "no": False, "1": True, "0": False})
                if (source.notna() & parsed.isna()).any():
                    raise ValueError(f"Cast to boolean would lose values in {col!r}")
                out[col] = parsed.astype("boolean")
    elif spec.type == "validate_constraint":
        return out
    return out


def execute_safely(frame: pd.DataFrame, raw_spec: dict[str, Any], *,
                   policies: dict[str, Any] | None = None, approved: bool = False) -> pd.DataFrame:
    try:
        spec = OperationSpec.model_validate(raw_spec)
        policies = policies or {}
        if spec.type in DESTRUCTIVE_ROW_TOOLS and not policies.get("allow_row_deletion", False):
            raise ValueError("Row deletion is prohibited by the active preparation policy")
        if spec.type in DESTRUCTIVE_COLUMN_TOOLS and not policies.get("allow_column_deletion", False):
            raise ValueError("Column deletion is prohibited by the active preparation policy")
        if spec.type in DESTRUCTIVE_ROW_TOOLS | DESTRUCTIVE_COLUMN_TOOLS and not approved:
            raise ValueError("This operation requires explicit human approval")
        if spec.type == "normalize_categories" and not policies.get("allow_category_mapping", False):
            raise ValueError("Category mapping is prohibited by the active policy")
        if spec.type == "fill_missing" and not policies.get("allow_imputation", False):
            raise ValueError("Imputation is prohibited by the active policy")
        return execute_operation(frame, raw_spec)
    except (ValidationError, ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"Operation rejected; prior dataset remains unchanged: {exc}") from exc
