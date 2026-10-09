import pandas as pd
import pytest
from pydantic import ValidationError

from app.domain.models import Job, JobStatus, transition
from app.specifications.schema import parse_yaml_spec
from app.tools.registry import execute_safely


def test_yaml_nested_schema_and_unknown_fields():
    spec = parse_yaml_spec('''version: "1.0"
dataset:
  objective: inspect transactions
schema:
  strict: true
  columns:
    amount:
      type: decimal
      nullable: false
      minimum: 0
quality_rules:
  - id: amount_present
    type: not_null
    columns: [amount]
policies:
  preserve_source: true
  allow_row_deletion: false
operations: []
''')
    assert spec.dataset.objective == "inspect transactions"
    assert spec.schema_.columns["amount"].minimum == 0
    with pytest.raises(ValidationError):
        parse_yaml_spec("operations: []\nunknown_field: true")


def test_transition_rejects_illegal_job_state():
    job = Job(source_file_id="s", source_hash="h", source_filename="x.csv", source_format="csv")
    assert transition(job, JobStatus.PROFILED).status == JobStatus.PROFILED
    with pytest.raises(ValueError):
        transition(job, JobStatus.COMPLETED)


def test_safe_operation_copy_and_invalid_argument_rejected():
    frame = pd.DataFrame({"name": [" Alice "]})
    spec = {"type": "trim_whitespace", "columns": ["name"], "arguments": {}}
    output = execute_safely(frame, spec)
    assert output.iloc[0, 0] == "Alice"
    assert frame.iloc[0, 0] == " Alice "
    with pytest.raises(ValueError, match="Unknown target columns"):
        execute_safely(frame, {"type": "trim_whitespace", "columns": ["missing"], "arguments": {}})


def test_row_deletion_requires_policy_and_approval():
    frame = pd.DataFrame({"name": ["a", "a"]})
    spec = {"type": "drop_duplicates", "columns": [], "arguments": {}}
    with pytest.raises(ValueError, match="prohibited"):
        execute_safely(frame, spec, approved=True)
    with pytest.raises(ValueError, match="approval"):
        execute_safely(frame, spec, policies={"allow_row_deletion": True})
    output = execute_safely(frame, spec, policies={"allow_row_deletion": True}, approved=True)
    assert len(output) == 1


def test_other_transformations_require_human_approval():
    frame = pd.DataFrame({"name": ["ADA"]})
    spec = {"type": "normalize_case", "columns": ["name"], "arguments": {"mode": "lower"}}
    with pytest.raises(ValueError, match="approval"):
        execute_safely(frame, spec)
    output = execute_safely(frame, spec, approved=True)
    assert output.loc[0, "name"] == "ada"


def test_imputation_requires_policy_and_human_approval():
    frame = pd.DataFrame({"amount": ["2", None, "4"]})
    spec = {"type": "fill_missing", "columns": ["amount"], "arguments": {"method": "mean"}}
    with pytest.raises(ValueError, match="approval"):
        execute_safely(frame, spec, policies={"allow_imputation": True})
    with pytest.raises(ValueError, match="prohibited"):
        execute_safely(frame, spec, approved=True)
    output = execute_safely(frame, spec, policies={"allow_imputation": True}, approved=True)
    assert float(output.loc[1, "amount"]) == 3.0


def test_placeholder_replacement_requires_policy_and_human_approval():
    frame = pd.DataFrame({"city": ["unknown", "Boston"]})
    spec = {"type": "replace_sentinels", "columns": ["city"], "arguments": {"values": ["unknown"]}}
    with pytest.raises(ValueError, match="approval"):
        execute_safely(frame, spec, policies={"allow_sentinel_replacement": True})
    with pytest.raises(ValueError, match="prohibited"):
        execute_safely(frame, spec, approved=True)
    output = execute_safely(frame, spec, policies={"allow_sentinel_replacement": True}, approved=True)
    assert pd.isna(output.loc[0, "city"])
    assert frame.loc[0, "city"] == "unknown"
