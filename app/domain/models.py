from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(StrEnum):
    UPLOADED = "UPLOADED"
    PROFILED = "PROFILED"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    VALIDATING = "VALIDATING"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    READY_TO_FINALIZE = "READY_TO_FINALIZE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class OperationStatus(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


class ReviewDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    EDIT = "EDIT"
    SKIP = "SKIP"
    DEFER = "DEFER"


class IssueSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class Job(BaseModel):
    model_config = ConfigDict(use_enum_values=True)
    job_id: str = Field(default_factory=lambda: str(uuid4()))
    source_file_id: str
    source_hash: str
    source_filename: str
    source_format: str
    selected_sheet: str | None = None
    objective: str = "Prepare for analysis"
    specification_version: str = "1"
    status: JobStatus = JobStatus.UPLOADED
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    current_run_id: str | None = None
    current_candidate_version: int = 0
    final_artifact_id: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    agent_step_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class PreparationOperation(BaseModel):
    operation_id: str = Field(default_factory=lambda: str(uuid4()))
    operation_type: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    target_columns: list[str] = Field(default_factory=list)
    input_version: int = 0
    output_version: int = 1
    status: OperationStatus = OperationStatus.PROPOSED
    created_by: str = "agent"
    reason: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0, le=1)
    risk_level: RiskLevel = RiskLevel.LOW
    requires_approval: bool = False
    validation_results: list[dict[str, Any]] = Field(default_factory=list)
    input_hash: str | None = None
    output_hash: str | None = None
    timestamp: datetime = Field(default_factory=utcnow)
    parent_operation_ids: list[str] = Field(default_factory=list)


class ValidationIssue(BaseModel):
    issue_id: str = Field(default_factory=lambda: str(uuid4()))
    rule_id: str
    severity: IssueSeverity
    category: str
    message: str
    affected_columns: list[str] = Field(default_factory=list)
    affected_row_ids: list[str] = Field(default_factory=list)
    affected_count: int = 0
    evidence: dict[str, Any] = Field(default_factory=dict)
    suggested_resolution: str | None = None
    requires_human_review: bool = False


class ReviewItem(BaseModel):
    review_id: str = Field(default_factory=lambda: str(uuid4()))
    job_id: str
    issue_type: str
    question: str
    candidate_values: list[Any] = Field(default_factory=list)
    supporting_evidence: dict[str, Any] = Field(default_factory=dict)
    affected_count: int = 0
    proposed_resolution: dict[str, Any] | None = None
    risk_level: RiskLevel
    status: str = "OPEN"
    decision: ReviewDecision | None = None
    decision_reason: str | None = None
    reviewed_at: datetime | None = None


ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    JobStatus.UPLOADED: {JobStatus.PROFILED, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.PROFILED: {JobStatus.PLANNING, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.PLANNING: {JobStatus.EXECUTING, JobStatus.NEEDS_REVIEW, JobStatus.READY_TO_FINALIZE, JobStatus.FAILED},
    JobStatus.EXECUTING: {JobStatus.VALIDATING, JobStatus.NEEDS_REVIEW, JobStatus.FAILED},
    JobStatus.VALIDATING: {JobStatus.READY_TO_FINALIZE, JobStatus.PLANNING, JobStatus.NEEDS_REVIEW, JobStatus.FAILED},
    JobStatus.NEEDS_REVIEW: {JobStatus.PLANNING, JobStatus.EXECUTING, JobStatus.READY_TO_FINALIZE, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.READY_TO_FINALIZE: {JobStatus.NEEDS_REVIEW, JobStatus.COMPLETED, JobStatus.FAILED},
    JobStatus.COMPLETED: set(),
    JobStatus.FAILED: set(),
    JobStatus.CANCELLED: set(),
}


def transition(job: Job, status: JobStatus) -> Job:
    current = JobStatus(job.status)
    if status not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"Invalid job transition: {current} -> {status}")
    return job.model_copy(update={"status": status, "updated_at": utcnow()})


def transition_payload(payload: dict[str, Any], status: JobStatus) -> dict[str, Any]:
    job = Job.model_validate(payload)
    updated = transition(job, status)
    return {**payload, "status": updated.status, "updated_at": updated.updated_at.isoformat()}
