import pandas as pd

from app.agent.graph import run_workflow
from app.agent.planner import EvidencePlanner, ScriptedPlanner
from app.domain.models import PreparationOperation


def test_graph_proposes_trim_and_waits_for_human_review():
    frame = pd.DataFrame({"name": [" Ada ", "Lin"]})
    result = run_workflow(frame, "prepare", EvidencePlanner(), max_steps=5)
    assert result["frame"]["name"].tolist() == [" Ada ", "Lin"]
    assert not result["ledger"]
    assert result["status"] == "NEEDS_REVIEW"
    assert result["review_required"] is True


def test_graph_routes_destructive_operation_to_review():
    frame = pd.DataFrame({"name": ["Ada", "Ada"]})
    result = run_workflow(frame, "dedupe", EvidencePlanner(), max_steps=5)
    assert result["status"] == "NEEDS_REVIEW"
    assert result["review_required"] is True
    assert len(result["frame"]) == 2


def test_unknown_tool_is_rejected_without_mutating_frame():
    operation = PreparationOperation(operation_type="unregistered_tool", reason="bad", target_columns=["name"])
    result = run_workflow(pd.DataFrame({"name": ["Ada"]}), "bad", ScriptedPlanner([operation.model_dump(mode="json")]))
    # The graph rejects the invalid planner contract at plan construction rather than executing code.
    assert result["frame"]["name"].tolist() == ["Ada"]
    assert result["status"] == "FAILED"
    assert result["failed_operations"][0]["status"] == "FAILED"
