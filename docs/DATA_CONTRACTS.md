# Data contracts

`app.domain.models` defines job, lifecycle, preparation operation, issue, review, risk, and decision contracts. Illegal lifecycle transitions raise `ValueError`. `app.specifications.schema` defines a versioned YAML contract with strict unknown-field rejection. Quality rules and operations are separate: quality rules express what must hold; operations describe bounded changes.

## Row identity and provenance

Each source row receives a deterministic UUIDv5 derived from source SHA-256 and source row position. Operations preserve the private `_source_row_id` field except for explicitly generated data (not currently supported). The operation ledger stores changed-cell source/output column names, row ID, hashes of before/after values, and removed source row IDs. `/jobs/<job_id>/lineage?row_id=…&column=…` resolves a cell to its current value and relevant operation IDs. Raw before/after values are not copied into the ledger.

## Operation status

Proposed actions remain `PROPOSED` until an authorized tool executes. Successful operations are `EXECUTED`; tool failures are retained as failed graph evidence and the prior candidate remains intact; rejected review actions become `REJECTED`. Rejected operations are excluded from replay.

## YAML example

```yaml
version: "1.0"
dataset:
  objective: Prepare transaction data
  expected_grain: one row per transaction
  primary_key: [transaction_id]
schema:
  strict: false
  columns:
    transaction_id: {type: string, nullable: false, unique: true}
    amount: {type: decimal, nullable: false, minimum: 0}
quality_rules:
  - {id: amount_present, type: not_null, columns: [amount], severity: critical}
policies:
  preserve_source: true
  allow_row_deletion: false
  allow_imputation: false
  allow_sentinel_replacement: false
operations:
  - type: trim_whitespace
    columns: [transaction_id]
    arguments: {}
execution:
  max_agent_steps: 20
  max_operations: 50
  fail_on_critical_validation: true
  require_user_final_approval: true
```

Supported operations are enumerated by `OperationSpec`; per-tool arguments are allowlisted. YAML cannot contain Python expressions. `drop_duplicates`, `drop_empty_rows`, and `drop_missing_rows` require both an enabling policy and a human approval. Observed leading/trailing whitespace is trimmed automatically. Imputation and standalone placeholder replacement require explicit policy permission and human approval. Other normalization and ambiguous operations require review. Sentinel markers included in a reviewed imputation or missing-row-removal operation are covered by that operation's corresponding policy and approval.
