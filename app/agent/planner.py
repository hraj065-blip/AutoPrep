from __future__ import annotations

from typing import Protocol

import pandas as pd

from app.domain.models import PreparationOperation, RiskLevel
from app.profiling.profiler import profile_dataset
from app.specifications.schema import OperationSpec
from app.agent.llm_client import GroqPlannerClient, OpenAIPlannerClient, PlannerProviderError, ResponsesAPIPlannerClient


class Planner(Protocol):
    def propose(self, frame: pd.DataFrame, objective: str) -> list[PreparationOperation]: ...


class EvidencePlanner:
    """Credential-free policy planner; only proposes reversible, evidence-supported actions."""

    def propose(self, frame: pd.DataFrame, objective: str) -> list[PreparationOperation]:
        profile_frame = frame.drop(columns=["_source_row_id"], errors="ignore")
        profile = profile_dataset(profile_frame)
        proposals: list[PreparationOperation] = []
        for col in profile["columns"]:
            count = col["whitespace_count"]
            if count:
                proposals.append(PreparationOperation(
                    operation_type="trim_whitespace", target_columns=[col["name"]],
                    reason="Remove observed leading/trailing whitespace while preserving non-string values.",
                    evidence={"observed_count": count, "objective": objective}, confidence=0.99,
                    risk_level=RiskLevel.LOW, requires_approval=False,
                ))
        if profile["duplicate_full_rows"]:
            proposals.append(PreparationOperation(
                operation_type="drop_duplicates", target_columns=[],
                arguments={"subset": list(profile_frame.columns), "keep": "first"},
                reason="Duplicate rows were detected; removal may discard source records.",
                evidence={"duplicate_count": profile["duplicate_full_rows"]}, confidence=1.0,
                risk_level=RiskLevel.HIGH, requires_approval=True,
            ))
        return proposals


class ScriptedPlanner:
    def __init__(self, operations: list[dict]):
        self.operations = operations

    def propose(self, frame: pd.DataFrame, objective: str) -> list[PreparationOperation]:
        return [PreparationOperation.model_validate(item) for item in self.operations]


class HostedPlanner:
    def __init__(self, client: ResponsesAPIPlannerClient, *, policies: dict | None = None):
        self.client = client
        self.policies = policies or {"allow_row_deletion": False, "allow_imputation": False,
                                     "allow_sentinel_replacement": False, "allow_category_mapping": False}

    def propose(self, frame: pd.DataFrame, objective: str) -> list[PreparationOperation]:
        profile = profile_dataset(frame)
        compact_columns = [{key: col[key] for key in ("name", "inferred_type", "type_confidence", "null_count",
            "null_rate", "distinct_count", "whitespace_count", "numeric_parse_success_rate", "numeric_summary") if key in col}
            for col in profile["columns"]]
        context = {"objective": objective, "profile": {"row_count": profile["row_count"],
            "column_count": profile["column_count"], "duplicate_full_rows": profile["duplicate_full_rows"],
            "columns": compact_columns, "issue_rules": sorted({issue["rule_id"] for issue in profile["issues"]})},
            "allowed_tools": ["trim_whitespace", "normalize_whitespace", "normalize_case", "parse_numeric",
                "parse_date", "rename_columns", "normalize_column_names", "drop_duplicates", "fill_missing",
                "normalize_categories", "replace_sentinels", "cast_type"], "policies": self.policies}
        response = self.client.propose_plan(context)
        proposals = []
        for item in response["operations"]:
            validated = OperationSpec.model_validate({"type": item["type"], "columns": item["columns"],
                                                       "arguments": item["arguments"]})
            requires_approval = validated.type not in {"trim_whitespace", "validate_constraint"}
            proposals.append(PreparationOperation(operation_type=validated.type,
                target_columns=validated.columns, arguments=validated.arguments,
                reason=str(item["reason"])[:500], evidence={"profile_issue_rules": context["profile"]["issue_rules"]},
                risk_level=RiskLevel.MEDIUM if requires_approval else RiskLevel.LOW,
                requires_approval=requires_approval, created_by="agent"))
        return proposals


def configured_planner() -> tuple[Planner, ResponsesAPIPlannerClient | None]:
    import os
    mode = os.environ.get("PREPPILOT_PLANNER", "scripted").lower()
    if mode == "scripted":
        return EvidencePlanner(), None
    if mode not in {"openai", "groq"}:
        raise PlannerProviderError(f"Unsupported PREPPILOT_PLANNER value: {mode!r}. Choose scripted, openai, or groq.")
    if os.environ.get("PREPPILOT_ALLOW_DATA_TO_LLM", "0").lower() not in {"1", "true", "yes"}:
        raise PlannerProviderError(f"Set PREPPILOT_ALLOW_DATA_TO_LLM=1 to explicitly permit sharing aggregate profiles with {mode.title()}.")
    client = GroqPlannerClient() if mode == "groq" else OpenAIPlannerClient()
    return HostedPlanner(client), client
