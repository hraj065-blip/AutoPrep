# Agent and workflow design

The runtime graph is implemented with the documented LangGraph `StateGraph` builder and `START`/`END` routing API. Its current nodes are planner, deterministic execution, validation, and safe stop. The evidence planner uses only profile facts and automatically trims observed leading/trailing whitespace; duplicate removal is proposed for review and requires row-deletion consent plus approval. The execution registry also enforces policy and approval gates for imputation and placeholder replacement. Other consequential or ambiguous transformations remain review-gated.

After each operation, the graph sends a value-free feedback summary into the next planning pass. Successful actions contribute the validation verdict, issue IDs/severities/messages, and recent operation types, columns, and statuses. A rejected tool action contributes its type, target columns, and rejection reason; the graph blocks an identical type/columns/arguments signature from being retried. The hosted planner is instructed to use that evidence to revise its next proposal. Retries share the same bounded step budget; policy, schema, approval, and deterministic execution gates still apply. Profiles and feedback omit cell values; column labels remain untrusted input. Offline planning stays deterministic and policy-bound.

The operation lifecycle is deliberately distinct:

1. **Proposal:** planner creates a typed operation and supporting evidence.
2. **Authorization:** policy and review state decide whether it may run.
3. **Execution:** the allowlisted registry validates arguments and produces a new frame.
4. **Verdict and feedback:** the validation engine checks structural invariants and specified quality rules, then passes a compact summary to the next planning pass.

The graph applies a maximum step count, an operation budget, conditional routing, and a safe stop. Each successful action creates a versioned ledger entry with input/output hashes. Failed tool calls preserve the last valid frame. Reproducible scripted planning does not require a network or key.

Cell text and headers are treated as data. The offline planner does not send them to an LLM. Optional OpenAI and Groq Responses API adapters receive a compact profile without top values or examples. Hosted planning requires a provider key and separate environment opt-in, returns strict structured output, and validates every proposal locally before use. The local planner remains the default.

If a hosted provider returns a runtime `PlannerProviderError`, job creation retries from the immutable source with the offline evidence planner and records a visible fallback notice with provider/model metadata. If that fallback also fails, the failed job retains its unchanged candidate. A provider-failed job can be restarted from a valid explicit YAML specification only when no operations, reviews, or candidate versions were created; other failed jobs remain terminal.
