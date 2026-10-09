from __future__ import annotations

import re
from typing import Any

import pandas as pd

from app.domain.models import IssueSeverity, ValidationIssue

SENTINELS = {"n/a", "na", "null", "none", "unknown", "-", "--", "?", "9999"}


def _name_hint(name: str) -> str:
    value = re.sub(r"[^0-9a-zA-Z]+", "_", str(name).strip()).strip("_").lower()
    return value or "column"


def _inferred_type(series: pd.Series, name: str) -> tuple[str, str, float]:
    nonnull = series.dropna()
    if not len(nonnull):
        return "empty", "unknown", 1.0
    samples = nonnull.astype(str)
    if re.search(r"(^|_)(id|key|code|zip|postal|phone)(_|$)", _name_hint(name)):
        return str(series.dtype), "identifier", 0.95
    numeric = pd.to_numeric(samples.str.replace(r"[$,]", "", regex=True), errors="coerce")
    if numeric.notna().mean() >= 0.9:
        return str(series.dtype), "numeric", float(numeric.notna().mean())
    dates = pd.to_datetime(samples, errors="coerce", format="mixed")
    if dates.notna().mean() >= 0.9:
        return str(series.dtype), "datetime", float(dates.notna().mean())
    unique = samples.nunique(dropna=True)
    role = "categorical" if unique <= min(50, max(10, len(samples) * 0.2)) else "text"
    if samples.str.lower().isin({"true", "false", "yes", "no", "0", "1"}).mean() > 0.9:
        role = "boolean_candidate"
    return str(series.dtype), role, 1.0


def profile_dataset(frame: pd.DataFrame) -> dict[str, Any]:
    columns: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for name in frame.columns:
        values = frame[name]
        nonnull = values.dropna()
        observed, inferred, confidence = _inferred_type(values, str(name))
        strings = values.astype("string")
        whitespace = int((strings.str.strip() != strings).fillna(False).sum())
        sentinels = sorted(set(strings.dropna().str.strip().str.lower()) & SENTINELS)
        numeric = pd.to_numeric(strings.str.replace(r"[$,]", "", regex=True), errors="coerce")
        numeric_rate = float(numeric.notna().sum() / max(1, values.notna().sum()))
        col = {
            "name": str(name), "suggested_name": _name_hint(str(name)),
            "observed_type": observed, "inferred_type": inferred,
            "alternative_types": ["text", "numeric", "datetime"],
            "type_confidence": round(confidence, 3), "non_null_count": int(values.notna().sum()),
            "null_count": int(values.isna().sum()), "null_rate": float(values.isna().mean()) if len(values) else 0.0,
            "distinct_count": int(values.nunique(dropna=True)),
            "top_values": values.value_counts(dropna=True).head(5).astype(str).to_dict(),
            "examples": nonnull.head(5).astype(str).tolist(), "whitespace_count": whitespace,
            "sentinel_candidates": sentinels,
            "numeric_parse_success_rate": numeric_rate,
            "semantic_evidence": ["column-name heuristic" if inferred == "identifier" else "value parsing heuristic"],
        }
        if inferred == "numeric" and numeric.notna().any():
            col["numeric_summary"] = {"min": float(numeric.min()), "max": float(numeric.max()),
                                       "median": float(numeric.median()), "std": float(numeric.std()) if numeric.count() > 1 else 0.0}
            q1, q3 = numeric.quantile([.25, .75])
            spread = q3 - q1
            outliers = int(((numeric < q1 - 1.5 * spread) | (numeric > q3 + 1.5 * spread)).sum())
            if outliers:
                issues.append(ValidationIssue(rule_id="potential_outliers", severity=IssueSeverity.WARNING,
                    category="distribution", message=f"{outliers} values fall outside the 1.5×IQR range.",
                    affected_columns=[str(name)], affected_count=outliers,
                    suggested_resolution="Review values in context; no values were changed.").model_dump(mode="json"))
        if values.isna().any():
            issues.append(ValidationIssue(rule_id="missing_values", severity=IssueSeverity.WARNING,
                category="completeness", message=f"{int(values.isna().sum())} missing values detected.",
                affected_columns=[str(name)], affected_count=int(values.isna().sum()),
                suggested_resolution="Choose an explicit missing-value policy.").model_dump(mode="json"))
        if whitespace:
            issues.append(ValidationIssue(rule_id="outer_whitespace", severity=IssueSeverity.INFO,
                category="formatting", message=f"{whitespace} values contain leading or trailing whitespace.",
                affected_columns=[str(name)], affected_count=whitespace,
                suggested_resolution="Trim whitespace after review.").model_dump(mode="json"))
        if sentinels:
            issues.append(ValidationIssue(rule_id="sentinel_candidates", severity=IssueSeverity.WARNING,
                category="missingness", message="Possible placeholder values require interpretation.",
                affected_columns=[str(name)], affected_count=int(strings.str.strip().str.lower().isin(sentinels).sum()),
                evidence={"values": sentinels}, suggested_resolution="Confirm which placeholders mean missing.",
                requires_human_review=True).model_dump(mode="json"))
        if 0 < numeric_rate < 0.9:
            issues.append(ValidationIssue(rule_id="mixed_numeric_parse", severity=IssueSeverity.WARNING,
                category="types", message=f"Only {numeric_rate:.0%} of non-null values parse as numeric.",
                affected_columns=[str(name)], affected_count=int(values.notna().sum() - numeric.notna().sum()),
                suggested_resolution="Inspect representative values and specify separators or keep as text.",
                requires_human_review=True).model_dump(mode="json"))
        if inferred == "datetime":
            date_text = strings.dropna().astype(str)
            ambiguous = date_text.str.match(r"^\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\s*$")
            ambiguous_rows = int(ambiguous.sum())
            if ambiguous_rows:
                issues.append(ValidationIssue(rule_id="ambiguous_dates", severity=IssueSeverity.WARNING,
                    category="dates", message=f"{ambiguous_rows} dates use a month/day order that may be ambiguous.",
                    affected_columns=[str(name)], affected_count=ambiguous_rows,
                    suggested_resolution="Confirm day-first or month-first interpretation before parsing.",
                    requires_human_review=True).model_dump(mode="json"))
        normalized_categories = {str(value).strip().casefold() for value in nonnull.astype(str)}
        if inferred == "categorical" and len(normalized_categories) < values.nunique(dropna=True):
            issues.append(ValidationIssue(rule_id="category_case_variants", severity=IssueSeverity.WARNING,
                category="categories", message="Values differ only by letter case or surrounding whitespace.",
                affected_columns=[str(name)], affected_count=int(values.nunique(dropna=True)-len(normalized_categories)),
                suggested_resolution="Review an explicit canonical category mapping.",
                requires_human_review=True).model_dump(mode="json"))
        columns.append(col)
    duplicate_rows = int(frame.duplicated().sum())
    if duplicate_rows:
        issues.append(ValidationIssue(rule_id="duplicate_rows", severity=IssueSeverity.WARNING,
            category="uniqueness", message=f"{duplicate_rows} duplicate full rows detected.",
            affected_count=duplicate_rows, suggested_resolution="Review duplicates; removal requires approval.",
            requires_human_review=True).model_dump(mode="json"))
    return {
        "row_count": len(frame), "column_count": len(frame.columns),
        "memory_bytes": int(frame.memory_usage(index=True, deep=True).sum()),
        "duplicate_full_rows": duplicate_rows,
        "empty_rows": int(frame.isna().all(axis=1).sum()),
        "empty_columns": [str(c) for c in frame.columns if frame[c].isna().all()],
        "constant_columns": [str(c) for c in frame.columns if frame[c].nunique(dropna=False) <= 1],
        "candidate_identifier_columns": [str(c) for c in frame.columns if
            re.search(r"(^|_)(id|key|code|zip|postal|phone)(_|$)", _name_hint(str(c))) or
            (frame[c].notna().any() and frame[c].nunique(dropna=True) / max(1, frame[c].notna().sum()) > 0.98)],
        "columns": columns, "issues": issues,
    }
