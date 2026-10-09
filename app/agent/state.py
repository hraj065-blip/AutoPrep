from typing import Any, TypedDict

import pandas as pd


class WorkflowState(TypedDict, total=False):
    frame: pd.DataFrame
    original_frame: pd.DataFrame
    last_valid_frame: pd.DataFrame
    objective: str
    planner: Any
    max_steps: int
    max_operations: int
    policies: dict[str, Any]
    steps: int
    plan: list[dict[str, Any]]
    next_operation: dict[str, Any] | None
    ledger: list[dict[str, Any]]
    validation: dict[str, Any]
    review_required: bool
    status: str
    error: str | None
    failed_operations: list[dict[str, Any]]
