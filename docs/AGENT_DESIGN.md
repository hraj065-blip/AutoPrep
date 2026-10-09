# Agent and workflow design

The runtime graph is implemented with the documented LangGraph `StateGraph` builder and `START`/`END` routing API. Its current nodes are planner, deterministic execution, validation, and safe stop. The evidence planner uses only profile facts and automatically trims observed leading/trailing whitespace; duplicate removal is proposed for review and requires row-deletion consent plus approval. The execution registry also enforces policy and approval gates for imputation and placeholder replacement. Other consequential or ambiguous transformations remain review-gated.

The operation lifecycle is deliberately distinct:

1. **Proposal:** planner creates a typed operation and supporting evidence.
2. **Authorization:** policy and review state decide whether it may run.
3. **Execution:** the allowlisted registry validates arguments and produces a new frame.
4. **Verdict:** the validation engine checks structural invariants and specified quality rules.

The graph applies a maximum step count, an operation budget, conditional routing, and a safe stop. Each successful action creates a versioned ledger entry with input/output hashes. Failed tool calls preserve the last valid frame. Reproducible scripted planning does not require a network or key.

Cell text and headers are treated as data. The offline planner does not send them to an LLM. The optional OpenAI Responses adapter receives a compact profile without top values or examples. It requires a separate environment opt-in, returns strict structured output, and every proposal is validated locally before use. The local planner remains the default.
