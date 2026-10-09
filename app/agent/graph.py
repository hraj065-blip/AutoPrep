from __future__ import annotations

import hashlib
import json
from typing import Any

import pandas as pd
from langgraph.graph import END, START, StateGraph

from app.agent.state import WorkflowState
from app.domain.models import OperationStatus
from app.tools.registry import execute_safely
from app.validation.engine import validate_candidate


def frame_hash(frame: pd.DataFrame) -> str:
    rows = []
    for row in frame.itertuples(index=False, name=None):
        cells = []
        for value in row:
            if pd.isna(value):
                cells.append(None)
            else:
                scalar = value.item() if hasattr(value, "item") else value
                cells.append({"type": type(scalar).__name__, "value": str(scalar)})
        rows.append(cells)
    canonical = json.dumps({"columns": [str(c) for c in frame.columns], "rows": rows},
                           ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _cell_diff(before: pd.DataFrame, after: pd.DataFrame, op: dict[str, Any]) -> dict[str, Any]:
    if "_source_row_id" not in before or "_source_row_id" not in after:
        return {}
    before_ix = before.set_index("_source_row_id", drop=False)
    after_ids = set(after["_source_row_id"].astype(str))
    before_ids = set(before["_source_row_id"].astype(str))
    evidence: dict[str, Any] = {"removed_source_row_ids": sorted(before_ids - after_ids)}
    targets = set(op.get("target_columns", []))
    if op.get("operation_type") == "rename_columns":
        mapping = op.get("arguments", {}).get("mapping", {})
        column_pairs = [(old, new) for old, new in mapping.items()]
    else:
        column_pairs = [(col, col) for col in targets if col in before.columns and col in after.columns]
    changed = []
    for _, row in after.iterrows():
        row_id = str(row["_source_row_id"])
        if row_id not in before_ids:
            continue
        original = before_ix.loc[row_id]
        for old_col, new_col in column_pairs:
            if old_col not in before.columns or new_col not in after.columns:
                continue
            left, right = original[old_col], row[new_col]
            equal = (pd.isna(left) and pd.isna(right)) if pd.isna(left) or pd.isna(right) else str(left) == str(right)
            if not equal:
                changed.append({"source_row_id": row_id, "source_column": old_col,
                    "output_column": new_col,
                    "original_value_hash": hashlib.sha256(str(left).encode()).hexdigest(),
                    "result_value_hash": hashlib.sha256(str(right).encode()).hexdigest()})
    evidence["cell_changes"] = changed
    return evidence


def build_workflow():
    def plan_node(state: WorkflowState) -> dict[str, Any]:
        if len(state.get("ledger", [])) >= state.get("max_operations", 50):
            return {"status": "SAFE_STOP", "error": "Maximum operation count reached."}
        if state.get("steps", 0) >= state["max_steps"]:
            return {"status": "SAFE_STOP", "error": "Maximum agent step count reached."}
        operations = state["planner"].propose(state["frame"], state["objective"])
        serialized = [op.model_dump(mode="json") for op in operations]
        completed = {entry["operation_id"] for entry in state.get("ledger", [])}
        serialized = [op for op in serialized if op["operation_id"] not in completed]
        pending = next((op for op in serialized if not op["requires_approval"]), None)
        review = any(op["requires_approval"] for op in serialized)
        return {"plan": serialized, "next_operation": pending, "review_required": review,
                "status": "NEEDS_REVIEW" if pending is None and review else ("READY_TO_FINALIZE" if not serialized else "EXECUTING"),
                "steps": state.get("steps", 0) + 1}

    def execute_node(state: WorkflowState) -> dict[str, Any]:
        op = state["next_operation"]
        if not op:
            return {"status": "READY_TO_FINALIZE"}
        tool_spec = {"type": op["operation_type"], "columns": op["target_columns"], "arguments": op["arguments"]}
        before = state["frame"]
        try:
            candidate = execute_safely(before, tool_spec, policies=state.get("policies", {}),
                                       approved=not op.get("requires_approval", False))
        except (ValueError, TypeError) as exc:
            failed = {**op, "status": "FAILED", "error": str(exc)[:500]}
            return {"next_operation": None, "failed_operations": state.get("failed_operations", []) + [failed],
                    "status": "FAILED", "error": "Operation rejected; the previous candidate was preserved."}
        op["evidence"] = {**op.get("evidence", {}), **_cell_diff(before, candidate, op)}
        op["input_hash"] = frame_hash(before)
        op["output_hash"] = frame_hash(candidate)
        op["status"] = OperationStatus.EXECUTED.value
        op["input_version"] = len(state.get("ledger", []))
        op["output_version"] = op["input_version"] + 1
        return {"frame": candidate, "last_valid_frame": before.copy(deep=True), "next_operation": None,
                "ledger": state.get("ledger", []) + [op], "status": "VALIDATING"}

    def validate_node(state: WorkflowState) -> dict[str, Any]:
        result = validate_candidate(state["original_frame"], state["frame"])
        if state.get("ledger"):
            ledger = list(state["ledger"])
            ledger[-1] = {**ledger[-1], "validation_results": result}
        else:
            ledger = []
        if state.get("failed_operations"):
            result["passed"] = False
            result["issues"].append({"rule_id": "operation_rejected", "severity": "ERROR",
                                     "message": state.get("error", "Operation was rejected.")})
            return {"validation": result, "ledger": ledger, "status": "FAILED"}
        if not result["passed"]:
            if ledger:
                ledger[-1] = {**ledger[-1], "status": OperationStatus.FAILED.value}
            return {"frame": state.get("last_valid_frame", state["original_frame"]),
                    "ledger": ledger, "validation": result, "status": "FAILED",
                    "error": "Candidate failed validation; the previous valid version was restored."}
        return {"ledger": ledger, "validation": result, "status": "PLANNING"}

    def route_plan(state: WorkflowState) -> str:
        if state.get("status") in {"SAFE_STOP", "NEEDS_REVIEW"}:
            return "end"
        return "execute" if state.get("next_operation") else "end"

    def route_validation(state: WorkflowState) -> str:
        if state.get("status") == "FAILED":
            return "end"
        return "plan" if state.get("steps", 0) < state["max_steps"] else "stop"

    def stop_node(state: WorkflowState) -> dict[str, Any]:
        return {"status": "SAFE_STOP", "error": "Maximum agent step count reached."}

    graph = StateGraph(WorkflowState)
    graph.add_node("plan", plan_node)
    graph.add_node("execute", execute_node)
    graph.add_node("validate", validate_node)
    graph.add_node("safe_stop", stop_node)
    graph.add_edge(START, "plan")
    graph.add_conditional_edges("plan", route_plan, {"execute": "execute", "end": END})
    graph.add_edge("execute", "validate")
    graph.add_conditional_edges("validate", route_validation, {"plan": "plan", "stop": "safe_stop", "end": END})
    graph.add_edge("safe_stop", END)
    return graph.compile()


def run_workflow(frame: pd.DataFrame, objective: str, planner: Any, max_steps: int = 5,
                 max_operations: int = 50, policies: dict[str, Any] | None = None) -> WorkflowState:
    original = frame.copy(deep=True)
    state: WorkflowState = {"frame": frame.copy(deep=True), "original_frame": original,
        "last_valid_frame": original.copy(deep=True),
        "objective": objective, "planner": planner, "max_steps": max_steps,
        "max_operations": max_operations, "policies": policies or {},
        "steps": 0, "plan": [], "next_operation": None, "ledger": [],
        "review_required": False, "status": "PLANNING", "error": None}
    return build_workflow().invoke(state, {"recursion_limit": max(8, max_steps * 4)})
