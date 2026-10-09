# Security and privacy

- Only CSV and XLSX extensions are accepted; XLSX requires the ZIP signature and parser success. Uploads are bounded by Flask request size and configurable row/column/sheet limits.
- Server-generated UUIDs are used for job and source identity; client filenames are display metadata only.
- The original upload is stored as immutable BLOB data and verified by SHA-256 during replay.
- YAML and planner arguments are schema-validated; transformations are selected from an allowlist. No generated Python, shell, or SQL is executed.
- State-changing forms use a session CSRF token. Flask signs its session with a generated local secret unless `SECRET_KEY` is configured.
- Candidate operations use copies. High-impact row or column deletion is blocked by default policy and requires explicit policy plus review.
- CSV/XLSX export prefixes formula-triggering string values (`=`, `+`, `-`, `@`, tab, carriage return) with an apostrophe. This is an export-only safety transformation; the source and candidate snapshots are unchanged.
- Logs contain job/operation IDs and events, not complete datasets. The optional hosted planner sends compact aggregate profiles only when an API key is configured and `PREPPILOT_ALLOW_DATA_TO_LLM=1` is explicitly enabled.
- Local development has no login and should stay bound to localhost. Vercel requires a shared access password and stable session secret, but it does not implement accounts or per-user authorization; all authenticated visitors share the same workspace. Do not use sensitive datasets or multi-tenant production workloads.
- Vercel deployments require PostgreSQL because function filesystems are read-only and temporary storage is not durable. The app defaults to 2 MB uploads on Vercel to stay below the 4.5 MB function request/response limit with overhead; source, candidate, and artifact bytes are stored in PostgreSQL, suitable only for small portfolio datasets.

The project is a local portfolio application and does not claim regulatory compliance or production security certification.
