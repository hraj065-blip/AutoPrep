import pandas as pd
from app.specifications.schema import PreparationSpec


def validate_candidate(before: pd.DataFrame, after: pd.DataFrame) -> dict[str, object]:
    issues: list[dict[str, object]] = []
    if list(after.columns) != list(before.columns):
        issues.append({"rule_id": "column_change", "severity": "WARNING", "message": "Column structure changed."})
    if after.columns.duplicated().any():
        issues.append({"rule_id": "duplicate_headers", "severity": "CRITICAL", "message": "Duplicate output headers."})
    if len(after) > len(before) * 2 and len(before):
        issues.append({"rule_id": "row_growth", "severity": "CRITICAL", "message": "Unexpected row growth."})
    return {"passed": not any(i["severity"] == "CRITICAL" for i in issues),
            "before_rows": len(before), "after_rows": len(after), "issues": issues}


def validate_specification(frame: pd.DataFrame, spec: PreparationSpec) -> dict[str, object]:
    issues: list[dict[str, object]] = []

    def add(rule: str, severity: str, message: str, columns: list[str] | None = None, count: int = 0):
        issues.append({"rule_id": rule, "severity": severity.upper(), "message": message,
                       "affected_columns": columns or [], "affected_count": int(count)})

    for name, contract in spec.schema_.columns.items():
        if name not in frame.columns:
            add("schema_column_missing", "critical", f"Required schema column {name!r} is missing.", [name])
            continue
        values = frame[name]
        if not contract.nullable and values.isna().any():
            add("schema_not_null", "critical", f"Column {name!r} contains nulls.", [name], values.isna().sum())
        if contract.unique and values.duplicated().any():
            add("schema_unique", "critical", f"Column {name!r} is not unique.", [name], values.duplicated().sum())
        if contract.minimum is not None or contract.maximum is not None:
            numeric = pd.to_numeric(values, errors="coerce")
            invalid = pd.Series(False, index=values.index)
            if contract.minimum is not None:
                invalid |= numeric < contract.minimum
            if contract.maximum is not None:
                invalid |= numeric > contract.maximum
            if invalid.any():
                add("schema_range", "error", f"Column {name!r} has values outside its declared range.", [name], invalid.sum())
        if contract.allowed_values is not None:
            invalid = values.notna() & ~values.isin(contract.allowed_values)
            if invalid.any():
                add("schema_allowed_values", "error", f"Column {name!r} contains values outside its allowed set.", [name], invalid.sum())
        if contract.type in {"integer", "decimal", "number"}:
            parsed = pd.to_numeric(values, errors="coerce")
            invalid = values.notna() & parsed.isna()
            if invalid.any():
                add("schema_type", "error", f"Column {name!r} contains values that are not {contract.type}.", [name], invalid.sum())
        elif contract.type in {"date", "datetime"}:
            formats = contract.accepted_formats or [None]
            parsed = pd.Series(False, index=values.index)
            for fmt in formats:
                parsed |= pd.to_datetime(values, errors="coerce", format=fmt, dayfirst=False).notna()
            invalid = values.notna() & ~parsed
            if invalid.any():
                add("schema_date", "error", f"Column {name!r} has unparseable dates.", [name], invalid.sum())

    for rule in spec.quality_rules:
        cols = rule.columns
        missing_cols = [c for c in cols if c not in frame.columns]
        if missing_cols:
            add(rule.id, rule.severity, f"Quality rule refers to missing columns: {missing_cols}.", missing_cols)
            continue
        if rule.type == "not_null":
            bad = frame[cols].isna().any(axis=1)
        elif rule.type == "unique":
            bad = frame.duplicated(subset=cols, keep=False)
        elif rule.type == "range" and cols:
            series = pd.to_numeric(frame[cols[0]], errors="coerce")
            bad = series.isna() & frame[cols[0]].notna()
            if rule.minimum is not None:
                bad |= series < rule.minimum
            if rule.maximum is not None:
                bad |= series > rule.maximum
        elif rule.type == "allowed_values" and cols:
            allowed = rule.values or []
            bad = frame[cols[0]].notna() & ~frame[cols[0]].isin(allowed)
        elif rule.type == "date_parse" and cols:
            parsed = pd.to_datetime(frame[cols[0]], errors="coerce", format="mixed")
            bad = frame[cols[0]].notna() & parsed.isna()
        elif rule.type == "row_count":
            bad = pd.Series([False] * len(frame), index=frame.index)
        else:
            continue
        if bad.any():
            add(rule.id, rule.severity, f"Quality rule {rule.id!r} failed.", cols, bad.sum())

    if spec.schema_.strict:
        extras = [str(c) for c in frame.columns if c != "_source_row_id" and c not in spec.schema_.columns]
        if extras:
            add("schema_strict_columns", "error", f"Unexpected columns present: {extras}.", extras, len(extras))
    return {"passed": not any(i["severity"] in {"ERROR", "CRITICAL"} for i in issues),
            "issues": issues, "row_count": len(frame), "column_count": len(frame.columns)}
