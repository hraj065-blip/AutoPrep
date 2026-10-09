from __future__ import annotations

import io
import hashlib
import json
import logging
import uuid
from typing import Any

import pandas as pd

from app.agent.graph import _cell_diff, frame_hash, run_workflow
from app.agent.planner import configured_planner
from app.domain.models import Job, JobStatus, OperationStatus, ReviewDecision, ReviewItem, RiskLevel, transition_payload
from app.ingestion.readers import read_dataset, validate_frame
from app.persistence import db
from app.profiling.profiler import profile_dataset
from app.specifications.schema import parse_yaml_spec
from app.tools.registry import execute_safely
from app.validation.engine import validate_candidate, validate_specification

logger = logging.getLogger(__name__)


_NAMESPACE = uuid.UUID("a0f0792c-15c1-47b4-91a0-e6dba0d23774")


def _df_bytes(frame: pd.DataFrame) -> bytes:
    stream = io.StringIO()
    frame.to_csv(stream, index=False, lineterminator="\n")
    return stream.getvalue().encode("utf-8")


def _artifact_bytes(frame: pd.DataFrame) -> bytes:
    export = frame.drop(columns=["_source_row_id"], errors="ignore").copy()
    formula_prefixes = ("=", "+", "-", "@", "\t", "\r")
    for column in export.columns:
        export[column] = export[column].map(
            lambda value: "'" + value if isinstance(value, str) and value.lstrip().startswith(formula_prefixes) else value
        )
    return _df_bytes(export)


def _frame_bytes(raw: bytes, file_format: str, sheet: str | None) -> pd.DataFrame:
    result = read_dataset(raw, f"source.{file_format}", sheet=sheet)
    frame = result.frame.copy()
    frame.insert(0, "_source_row_id", [str(uuid.uuid5(_NAMESPACE, f"{result.sha256}:{result.selected_sheet or 'csv'}:{i}")) for i in range(len(frame))])
    return frame


def create_job(database: str, raw: bytes, filename: str, objective: str, sheet: str | None,
               max_rows: int, max_columns: int, max_agent_steps: int, max_sheets: int = 30,
               max_operations: int = 50) -> dict[str, Any]:
    planner, hosted_client = configured_planner()
    read = read_dataset(raw, filename, sheet, max_sheets)
    validate_frame(read.frame, max_rows, max_columns)
    frame = read.frame.copy()
    frame.insert(0, "_source_row_id", [str(uuid.uuid5(_NAMESPACE, f"{read.sha256}:{read.selected_sheet or 'csv'}:{i}")) for i in range(len(frame))])
    job = Job(source_file_id=read.source_file_id, source_hash=read.sha256,
        source_filename=filename[:255], source_format=read.format, selected_sheet=read.selected_sheet,
        objective=objective[:500]).model_dump(mode="json")
    job["current_run_id"] = str(uuid.uuid4())
    job["metadata"] = {"delimiter": read.delimiter, "encoding": read.encoding, "warnings": read.warnings}
    db.insert_job(database, job, raw)
    db.store_dataset_version(database, job["job_id"], 0, raw, None)
    profile = profile_dataset(frame.drop(columns=["_source_row_id"]))
    job = transition_payload(job, JobStatus.PROFILED)
    db.update_job(database, job["job_id"], payload=job, profile=profile, event=("profiled", {"rows": len(frame), "columns": len(frame.columns)-1}))
    job = transition_payload(job, JobStatus.PLANNING)
    db.update_job(database, job["job_id"], payload=job, event=("planning_started", {}))
    try:
        result = run_workflow(frame, objective, planner, max_agent_steps, max_operations)
    except Exception:
        logger.exception("PrepPilot workflow failed", extra={"job_id": job["job_id"], "event_type": "workflow_failure"})
        job = transition_payload(job, JobStatus.FAILED)
        job["failure_code"] = "LLM_PROVIDER_ERROR" if hosted_client else "INTERNAL_ERROR"
        job["failure_message"] = "The preparation workflow stopped before producing a candidate. Check the provider configuration or application log."
        job["metadata"]["workflow_status"] = "FAILED"
        db.update_job(database, job["job_id"], payload=job, candidate=_df_bytes(frame),
            event=("workflow_failed", {"failure_code": job["failure_code"]}))
        return get_job(database, job["job_id"])
    plan = result.get("plan", [])
    ledger = result.get("ledger", [])
    reviews = []
    for operation in plan:
        if operation.get("requires_approval"):
            review = ReviewItem(job_id=job["job_id"], issue_type=operation["operation_type"],
                question=f"Approve operation: {operation['reason']}", candidate_values=[],
                supporting_evidence=operation.get("evidence", {}),
                affected_count=int(operation.get("evidence", {}).get("duplicate_count",
                    operation.get("evidence", {}).get("observed_count", 0))),
                proposed_resolution=operation, risk_level=RiskLevel(operation["risk_level"])).model_dump(mode="json")
            reviews.append(review)
    status = (JobStatus.FAILED if result.get("status") in {"SAFE_STOP", "FAILED"} else
              JobStatus.NEEDS_REVIEW if reviews else JobStatus.READY_TO_FINALIZE)
    job = transition_payload(job, status)
    job["agent_step_count"] = int(result.get("steps", 0))
    executed_ledger = [op for op in ledger if op.get("status") == OperationStatus.EXECUTED.value]
    job["current_candidate_version"] = len(executed_ledger)
    job["metadata"]["workflow_status"] = result.get("status")
    if hosted_client:
        job["metadata"]["planner"] = {"provider": hosted_client.provider, "model": hosted_client.model,
                                         "usage": hosted_client.last_usage}
    if result.get("error"):
        job["metadata"]["workflow_message"] = result["error"]
    if status == JobStatus.FAILED:
        job["failure_code"] = "workflow_safe_stop"
        job["failure_message"] = "The workflow stopped safely before producing a final candidate."
    candidate = _df_bytes(result.get("frame", frame))
    failed_operations = result.get("failed_operations", [])
    db.update_job(database, job["job_id"], payload=job, candidate=candidate,
        operations=ledger + failed_operations + [r["proposed_resolution"] for r in reviews], reviews=reviews,
        event=("workflow_finished", {"status": status.value, "steps": job["agent_step_count"], "executed_operations": len(executed_ledger)}))
    snapshot = frame.copy(deep=True)
    for operation in executed_ledger:
        spec = {"type": operation["operation_type"], "columns": operation["target_columns"], "arguments": operation["arguments"]}
        snapshot = execute_safely(snapshot, spec)
        db.store_dataset_version(database, job["job_id"], operation["output_version"],
            _df_bytes(snapshot), operation["operation_id"])
    if not executed_ledger:
        db.store_dataset_version(database, job["job_id"], 0, _df_bytes(result.get("frame", frame)), None)
    return get_job(database, job["job_id"])


def get_job(database: str, job_id: str) -> dict[str, Any] | None:
    return db.get_job(database, job_id)


def source_frame(job: dict[str, Any]) -> pd.DataFrame:
    raw = job["source"]
    payload = job["payload"]
    return _frame_bytes(raw, payload["source_format"], payload.get("selected_sheet"))


def candidate_frame(job: dict[str, Any]) -> pd.DataFrame:
    raw = job["candidate"]
    return pd.read_csv(io.BytesIO(raw), dtype=object) if raw else source_frame(job)


def decide_review(database: str, job_id: str, review_id: str, decision: str,
                  reason: str, max_rows: int, max_columns: int, max_agent_steps: int = 5,
                  arguments_json: str | None = None, columns_json: str | None = None,
                  allow_row_deletion: bool = False, allow_imputation: bool = False,
                  allow_sentinel_replacement: bool = False) -> dict[str, Any]:
    item = get_job(database, job_id)
    if not item:
        raise KeyError("Job not found")
    reviews = item["reviews"]
    review = next((r for r in reviews if r["review_id"] == review_id), None)
    if not review or review["status"] != "OPEN":
        raise ValueError("Review item is unavailable or already resolved")
    decision_enum = ReviewDecision(decision)
    if decision_enum == ReviewDecision.DEFER:
        review.setdefault("decision_history", []).append({"decision": "DEFER", "reason": reason[:1000]})
        review["decision"] = "DEFER"
        review["decision_reason"] = reason[:1000]
        from datetime import datetime, timezone
        review["reviewed_at"] = datetime.now(timezone.utc).isoformat()
        db.update_job(database, job_id, reviews=reviews, event=("review_deferred", {"review_id": review_id}))
        return get_job(database, job_id)
    operation = review["proposed_resolution"]
    operations = item["operations"]
    if decision_enum in {ReviewDecision.APPROVE, ReviewDecision.EDIT}:
        frame = candidate_frame(item)
        if decision_enum == ReviewDecision.EDIT:
            from app.specifications.schema import OperationSpec
            try:
                edited_arguments = json.loads(arguments_json or "{}")
                edited_columns = json.loads(columns_json or json.dumps(operation["target_columns"]))
            except json.JSONDecodeError as exc:
                raise ValueError("Edited operation arguments must be valid JSON.") from exc
            checked = OperationSpec.model_validate({"type": operation["operation_type"],
                "columns": edited_columns, "arguments": edited_arguments})
            operation["arguments"] = checked.arguments
            operation["target_columns"] = checked.columns
        input_hash = frame_hash(frame)
        tool = {"type": operation["operation_type"], "columns": operation["target_columns"], "arguments": operation["arguments"]}
        policies = dict(item["payload"].get("metadata", {}).get("policies", {}))
        row_deletion_authorized = operation["operation_type"] in {"drop_duplicates", "drop_empty_rows", "drop_missing_rows"} and allow_row_deletion
        imputation_authorized = operation["operation_type"] == "fill_missing" and allow_imputation
        sentinel_replacement_authorized = operation["operation_type"] == "replace_sentinels" and allow_sentinel_replacement
        if row_deletion_authorized:
            policies["allow_row_deletion"] = True
        if imputation_authorized:
            policies["allow_imputation"] = True
        if sentinel_replacement_authorized:
            policies["allow_sentinel_replacement"] = True
        candidate = execute_safely(frame, tool,
            policies=policies, approved=True)
        validate_frame(candidate.drop(columns=["_source_row_id"], errors="ignore"), max_rows, max_columns)
        validation = validate_candidate(source_frame(item), candidate)
        if not validation["passed"]:
            operation["status"] = OperationStatus.FAILED.value
            operation["validation_results"] = [validation]
            operations = [operation if op.get("operation_id") == operation["operation_id"] else op for op in operations]
            item["payload"] = transition_payload(item["payload"], JobStatus.FAILED)
            item["payload"]["failure_code"] = "validation_failure"
            item["payload"]["failure_message"] = "Approved operation failed validation; the previous candidate was preserved."
            review["status"] = "RESOLVED"
            review["decision"] = decision_enum.value
            review["decision_reason"] = reason[:1000]
            db.update_job(database, job_id, payload=item["payload"], operations=operations,
                reviews=reviews, event=("approved_operation_rolled_back", {"review_id": review_id, "validation": validation}))
            return get_job(database, job_id)
        operation["input_hash"] = input_hash
        operation["output_hash"] = frame_hash(candidate)
        operation["evidence"] = {**operation.get("evidence", {}), **_cell_diff(frame, candidate, operation)}
        operation["validation_results"] = [validation]
        operation["status"] = OperationStatus.EXECUTED.value
        operation["created_by"] = "user"
        operation["input_version"] = item["payload"]["current_candidate_version"]
        operation["output_version"] = operation["input_version"] + 1
        operations = [operation if op.get("operation_id") == operation["operation_id"] else op for op in operations]
        db.update_job(database, job_id, candidate=_df_bytes(candidate))
        if row_deletion_authorized or imputation_authorized or sentinel_replacement_authorized:
            item["payload"].setdefault("metadata", {})["policies"] = policies
        db.store_dataset_version(database, job_id, operation["output_version"],
                                 _df_bytes(candidate), operation["operation_id"])
        item["payload"]["current_candidate_version"] = operation["output_version"]
    else:
        operation["status"] = OperationStatus.REJECTED.value
        operations = [operation if op.get("operation_id") == operation["operation_id"] else op for op in operations]
    review["status"] = "RESOLVED"
    review["decision"] = decision_enum.value
    review["decision_reason"] = reason[:1000]
    from datetime import datetime, timezone
    review["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    open_reviews = any(r["status"] == "OPEN" for r in reviews)
    new_status = JobStatus.NEEDS_REVIEW.value if open_reviews else JobStatus.READY_TO_FINALIZE.value
    if new_status != JobStatus.NEEDS_REVIEW.value:
        item["payload"] = transition_payload(item["payload"], JobStatus(new_status))
    db.update_job(database, job_id, payload=item["payload"], operations=operations, reviews=reviews,
        event=("review_decided", {"review_id": review_id, "decision": decision_enum.value,
            "row_deletion_authorized": bool(decision_enum in {ReviewDecision.APPROVE, ReviewDecision.EDIT}
                and operation["operation_type"] in {"drop_duplicates", "drop_empty_rows", "drop_missing_rows"}
                and item["payload"].get("metadata", {}).get("policies", {}).get("allow_row_deletion", False)),
            "imputation_authorized": bool(decision_enum in {ReviewDecision.APPROVE, ReviewDecision.EDIT}
                and operation["operation_type"] == "fill_missing"
                and item["payload"].get("metadata", {}).get("policies", {}).get("allow_imputation", False)),
            "sentinel_replacement_authorized": bool(decision_enum in {ReviewDecision.APPROVE, ReviewDecision.EDIT}
                and operation["operation_type"] == "replace_sentinels"
                and item["payload"].get("metadata", {}).get("policies", {}).get("allow_sentinel_replacement", False))}))
    if decision_enum == ReviewDecision.APPROVE and not open_reviews:
        from app.agent.planner import EvidencePlanner
        resumed = run_workflow(candidate, item["payload"]["objective"], EvidencePlanner(),
                               max_steps=max_agent_steps,
                               policies=item["payload"].get("metadata", {}).get("policies", {}))
        offset = item["payload"].get("current_candidate_version", 0)
        latest_ops = resumed.get("ledger", [])
        for op in latest_ops:
            op["input_version"] += offset
            op["output_version"] += offset
            if operations:
                op["parent_operation_ids"] = [operations[-1]["operation_id"]]
        next_reviews = []
        for op in resumed.get("plan", []):
            if op.get("requires_approval"):
                next_reviews.append(ReviewItem(job_id=job_id, issue_type=op["operation_type"],
                    question=f"Approve operation: {op['reason']}", supporting_evidence=op.get("evidence", {}),
                    affected_count=int(op.get("evidence", {}).get("duplicate_count", 0)),
                    proposed_resolution=op, risk_level=RiskLevel(op["risk_level"])).model_dump(mode="json"))
        prior_operation = operations[-1]["operation_id"] if operations else None
        for op in latest_ops:
            if prior_operation:
                op["parent_operation_ids"] = [prior_operation]
            prior_operation = op["operation_id"]
        operations.extend(latest_ops)
        operations.extend(resumed.get("failed_operations", []))
        reviews.extend(next_reviews)
        item["payload"]["current_candidate_version"] = offset + sum(op.get("status") == OperationStatus.EXECUTED.value for op in latest_ops)
        item["payload"]["agent_step_count"] += int(resumed.get("steps", 0))
        item["payload"]["current_run_id"] = str(uuid.uuid4())
        resumed_status = (JobStatus.NEEDS_REVIEW if next_reviews else
            JobStatus.FAILED if resumed.get("status") in {"FAILED", "SAFE_STOP"} else JobStatus.READY_TO_FINALIZE)
        if resumed_status != JobStatus(item["payload"]["status"]):
            item["payload"] = transition_payload(item["payload"], resumed_status)
        db.update_job(database, job_id, payload=item["payload"], candidate=_df_bytes(resumed.get("frame", candidate)),
            operations=operations, reviews=reviews,
            event=("workflow_resumed", {"status": item["payload"]["status"], "step_count": resumed.get("steps", 0)}))
        snapshot = candidate.copy(deep=True)
        for op in latest_ops:
            if op.get("status") == OperationStatus.EXECUTED.value:
                tool = {"type": op["operation_type"], "columns": op["target_columns"], "arguments": op["arguments"]}
                snapshot = execute_safely(snapshot, tool, policies=item["payload"].get("metadata", {}).get("policies", {}))
                db.store_dataset_version(database, job_id, op["output_version"], _df_bytes(snapshot), op["operation_id"])
    return get_job(database, job_id)


def finalize_job(database: str, job_id: str) -> dict[str, Any]:
    item = get_job(database, job_id)
    if not item:
        raise KeyError("Job not found")
    if item["payload"]["status"] != JobStatus.READY_TO_FINALIZE.value:
        raise ValueError("Job must be ready to finalize and have no open review items.")
    if any(r["status"] == "OPEN" for r in item["reviews"]):
        raise ValueError("Open review items must be resolved before finalization.")
    specification = item["payload"].get("metadata", {}).get("specification")
    structural = validate_candidate(source_frame(item), candidate_frame(item))
    item["payload"]["metadata"]["structural_validation"] = structural
    if not structural["passed"]:
        item["payload"]["status"] = JobStatus.FAILED.value
        item["payload"]["failure_code"] = "final_quality_gate_failed"
        item["payload"]["failure_message"] = "Structural validation failed; no trusted export was created."
        db.update_job(database, job_id, payload=item["payload"], event=("final_quality_gate_blocked", structural))
        raise ValueError(item["payload"]["failure_message"])
    if specification:
        from app.specifications.schema import PreparationSpec
        parsed = PreparationSpec.model_validate(specification)
        report = validate_specification(candidate_frame(item).drop(columns=["_source_row_id"], errors="ignore"), parsed)
        item["payload"]["metadata"]["validation"] = report
        if any(issue["severity"] in {"ERROR", "CRITICAL"} for issue in report["issues"]):
            item["payload"] = transition_payload(item["payload"], JobStatus.FAILED)
            item["payload"]["failure_code"] = "final_quality_gate_failed"
            item["payload"]["failure_message"] = "The final quality gate failed; no trusted export was created."
            db.update_job(database, job_id, payload=item["payload"], event=("final_quality_gate_blocked", report))
            raise ValueError(item["payload"]["failure_message"])
    artifact = _artifact_bytes(candidate_frame(item))
    item["payload"] = transition_payload(item["payload"], JobStatus.COMPLETED)
    item["payload"]["final_artifact_id"] = str(uuid.uuid4())
    db.update_job(database, job_id, payload=item["payload"], artifact=artifact,
                  event=("finalized", {"artifact_id": item["payload"]["final_artifact_id"]}))
    return get_job(database, job_id)


def apply_yaml_spec(database: str, job_id: str, yaml_text: str, max_steps: int) -> dict[str, Any]:
    from app.agent.planner import ScriptedPlanner
    from app.domain.models import PreparationOperation
    spec = parse_yaml_spec(yaml_text)
    item = get_job(database, job_id)
    if not item:
        raise KeyError("Job not found")
    if item["payload"]["status"] not in {JobStatus.NEEDS_REVIEW.value, JobStatus.READY_TO_FINALIZE.value}:
        raise ValueError("A YAML spec can be applied after the current preparation run reaches a safe stop.")
    if len(spec.operations) > spec.execution.max_operations:
        raise ValueError("Specification operation count exceeds its declared limit.")
    max_steps = min(max_steps, spec.execution.max_agent_steps)
    frame = candidate_frame(item)
    ops = []
    for op in spec.operations:
        requires_approval = op.type not in {"trim_whitespace", "validate_constraint"}
        evidence: dict[str, Any] = {}
        if op.type == "drop_missing_rows":
            subset = op.columns or [column for column in frame.columns if column != "_source_row_id"]
            missing = frame[subset].isna().copy()
            sentinels = {str(value).strip().casefold() for value in op.arguments.get("sentinels", [])}
            for column in subset:
                if sentinels:
                    normalized = frame[column].map(lambda value: str(value).strip().casefold() if pd.notna(value) else "")
                    missing[column] |= normalized.isin(sentinels)
            evidence["missing_row_count"] = int(missing.any(axis=1).sum())
        elif op.type == "fill_missing":
            missing = frame[op.columns].isna().copy()
            sentinels = {str(value).strip().casefold() for value in op.arguments.get("sentinels", [])}
            for column in op.columns:
                if sentinels:
                    normalized = frame[column].map(lambda value: str(value).strip().casefold() if pd.notna(value) else "")
                    missing[column] |= normalized.isin(sentinels)
            evidence["missing_values_count"] = int(missing.sum().sum())
        elif op.type == "replace_sentinels":
            sentinels = set(op.arguments.get("values", []))
            evidence["placeholder_count"] = int(sum(frame[column].isin(sentinels).sum() for column in op.columns))
        reason = "Explicit operation from user supplied YAML specification."
        if op.type == "fill_missing":
            reason = f"Fill missing values in {', '.join(op.columns)} using {op.arguments.get('method', 'an explicit value')} and review the result."
        elif op.type == "drop_missing_rows":
            reason = f"Remove rows with missing values in {', '.join(op.columns)} and review the resulting row count."
        elif op.type == "replace_sentinels":
            reason = f"Replace selected placeholder values in {', '.join(op.columns)} with missing values, then review the result."
        if op.arguments.get("sentinels"):
            reason += f" Treat these selected placeholder markers as missing: {', '.join(op.arguments['sentinels'])}."
        ops.append(PreparationOperation(operation_type=op.type, target_columns=op.columns,
            arguments=op.arguments, reason=reason, evidence=evidence,
            created_by="user", risk_level=RiskLevel.MEDIUM if requires_approval else RiskLevel.LOW,
            requires_approval=requires_approval).model_dump(mode="json"))
    result = run_workflow(frame, spec.objective, ScriptedPlanner(ops), max_steps,
                          max_operations=spec.execution.max_operations,
                          policies=spec.policies.model_dump())
    all_ledger = item["operations"] + result.get("ledger", [])
    reviews = item["reviews"]
    for operation in result.get("plan", []):
        if operation.get("requires_approval"):
            reviews.append(ReviewItem(job_id=job_id, issue_type=operation["operation_type"],
                question=f"Approve YAML operation: {operation['reason']}",
                supporting_evidence=operation.get("evidence", {}),
                affected_count=operation.get("evidence", {}).get("duplicate_count",
                    operation.get("evidence", {}).get("missing_row_count",
                    operation.get("evidence", {}).get("missing_values_count",
                    operation.get("evidence", {}).get("placeholder_count", 0)))),
                proposed_resolution=operation, risk_level=RiskLevel.HIGH).model_dump(mode="json"))
    item["payload"]["objective"] = spec.objective
    item["payload"]["specification_version"] = spec.version
    item["payload"]["metadata"]["specification"] = spec.model_dump(mode="json", by_alias=True)
    prior_policies = item["payload"]["metadata"].get("policies", {})
    declared_policies = spec.policies.model_dump(mode="json")
    item["payload"]["metadata"]["policies"] = {
        key: bool(prior_policies.get(key, False) or declared_policies.get(key, False))
        if key in {"allow_row_deletion", "allow_imputation", "allow_sentinel_replacement"} else value
        for key, value in declared_policies.items()}
    item["payload"]["metadata"]["execution"] = spec.execution.model_dump(mode="json")
    spec_status = (JobStatus.FAILED if result.get("status") in {"SAFE_STOP", "FAILED"} else
        JobStatus.NEEDS_REVIEW if any(r["status"] == "OPEN" for r in reviews) else JobStatus.READY_TO_FINALIZE)
    if spec_status != JobStatus(item["payload"]["status"]):
        item["payload"] = transition_payload(item["payload"], spec_status)
    successful_yaml_ops = [op for op in result.get("ledger", []) if op.get("status") == OperationStatus.EXECUTED.value]
    offset = item["payload"].get("current_candidate_version", 0)
    for operation in result.get("ledger", []):
        operation["input_version"] += offset
        operation["output_version"] += offset
        if item["operations"]:
            operation["parent_operation_ids"] = [item["operations"][-1]["operation_id"]]
    item["payload"]["current_candidate_version"] += len(successful_yaml_ops)
    candidate = result.get("frame", frame)
    validation = validate_specification(candidate.drop(columns=["_source_row_id"], errors="ignore"), spec)
    item["payload"]["metadata"]["validation"] = validation
    unresolved_reviews = any(r["status"] == "OPEN" for r in reviews)
    if spec.execution.fail_on_critical_validation and any(i["severity"] == "CRITICAL" for i in validation["issues"]) and not unresolved_reviews:
        item["payload"] = transition_payload(item["payload"], JobStatus.FAILED)
        item["payload"]["failure_code"] = "critical_validation_failure"
        item["payload"]["failure_message"] = "A critical specification rule failed; finalization is blocked."
    db.update_job(database, job_id, payload=item["payload"], candidate=_df_bytes(candidate),
        operations=all_ledger, reviews=reviews,
        event=("yaml_spec_applied", {"version": spec.version, "operations": len(ops)}))
    snapshot = frame.copy(deep=True)
    for operation in successful_yaml_ops:
        tool = {"type": operation["operation_type"], "columns": operation["target_columns"], "arguments": operation["arguments"]}
        snapshot = execute_safely(snapshot, tool, policies=spec.policies.model_dump())
        db.store_dataset_version(database, job_id, operation["output_version"],
            _df_bytes(snapshot), operation["operation_id"])
    return get_job(database, job_id)


def replay_job(database: str, job_id: str) -> pd.DataFrame:
    item = get_job(database, job_id)
    if not item:
        raise KeyError("Job not found")
    actual_source_hash = hashlib.sha256(item["source"]).hexdigest()
    if actual_source_hash != item["payload"]["source_hash"]:
        raise ValueError("REPLAY_MISMATCH: immutable source hash verification failed.")
    frame = source_frame(item)
    for operation in item["operations"]:
        if operation.get("status") != OperationStatus.EXECUTED.value:
            continue
        if operation.get("input_hash") and frame_hash(frame) != operation["input_hash"]:
            raise ValueError(f"REPLAY_MISMATCH: input hash differs before operation {operation['operation_id']}.")
        spec = {"type": operation["operation_type"], "columns": operation["target_columns"], "arguments": operation["arguments"]}
        frame = execute_safely(frame, spec,
            policies=item["payload"].get("metadata", {}).get("policies", {}), approved=True)
        if operation.get("output_hash") and frame_hash(frame) != operation["output_hash"]:
            raise ValueError(f"REPLAY_MISMATCH: output hash differs after operation {operation['operation_id']}.")
    return frame


def cell_provenance(job: dict[str, Any], row_id: str, column: str) -> dict[str, Any]:
    frame = candidate_frame(job)
    matches = frame.index[frame["_source_row_id"].astype(str) == row_id].tolist() if "_source_row_id" in frame else []
    if not matches:
        return {"source_row_id": row_id, "column": column, "present": False,
                "note": "The source row was removed by an approved operation or is unavailable."}
    operations = []
    changed_cells = []
    for op in job["operations"]:
        cols = set(op.get("target_columns", []))
        if op.get("operation_type") == "rename_columns":
            cols |= set(op.get("arguments", {}).get("mapping", {}).keys()) | set(op.get("arguments", {}).get("mapping", {}).values())
        cell_records = [change for change in op.get("evidence", {}).get("cell_changes", [])
                        if change.get("source_row_id") == row_id and change.get("output_column") == column]
        if cell_records:
            changed_cells.extend(cell_records)
            operations.append(op["operation_id"])
        elif op.get("status") == OperationStatus.EXECUTED.value and (column in cols or not cols):
            operations.append(op["operation_id"])
    value = frame.iloc[matches[0]][column] if column in frame else None
    return {"source_row_id": row_id, "column": column, "present": True,
            "current_value": None if pd.isna(value) else str(value), "operation_ids": operations,
            "changed_cell_evidence": changed_cells}
