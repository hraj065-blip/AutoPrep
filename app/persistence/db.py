from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class _HybridRow(dict):
    """A psycopg row supporting both named and positional access like sqlite3.Row."""

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def _postgres_row(cursor: Any) -> Any:
    def make_row(values: tuple[Any, ...]) -> Any:
        description = cursor.description
        if not description:
            return values
        return _HybridRow((column.name, value) for column, value in zip(description, values))

    return make_row


class _PostgresConnection:
    def __init__(self, url: str) -> None:
        import psycopg

        self._connection = psycopg.connect(url, row_factory=_postgres_row)

    def __enter__(self) -> _PostgresConnection:
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            self._connection.__exit__(exc_type, exc, traceback)
        finally:
            self._connection.close()

    def execute(self, query: str, parameters: tuple[Any, ...] = ()) -> Any:
        return self._connection.execute(query.replace("?", "%s"), parameters)

    def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            if statement.strip():
                self._connection.execute(statement)


def _is_postgres(path: str) -> bool:
    return path.startswith(("postgres://", "postgresql://"))


def connect(path: str) -> Any:
    if _is_postgres(path):
        # Hosted providers commonly expose the legacy postgres:// URL scheme.
        url = "postgresql://" + path[len("postgres://"):] if path.startswith("postgres://") else path
        return _PostgresConnection(url)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def initialize(path: str) -> None:
    if _is_postgres(path):
        schema = """
        CREATE TABLE IF NOT EXISTS jobs (
          job_id TEXT PRIMARY KEY, source BYTEA NOT NULL, payload TEXT NOT NULL,
          profile TEXT, candidate BYTEA, operations TEXT NOT NULL DEFAULT '[]',
          reviews TEXT NOT NULL DEFAULT '[]', artifact BYTEA
        );
        CREATE TABLE IF NOT EXISTS audit_events (
          event_id BIGSERIAL PRIMARY KEY, job_id TEXT NOT NULL,
          event_type TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(job_id) REFERENCES jobs(job_id)
        );
        CREATE INDEX IF NOT EXISTS idx_audit_job ON audit_events(job_id, event_id);
        CREATE TABLE IF NOT EXISTS evaluation_runs (
          run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, status TEXT NOT NULL,
          summary TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dataset_versions (
          job_id TEXT NOT NULL, version_number INTEGER NOT NULL,
          operation_id TEXT, content_hash TEXT NOT NULL, artifact BYTEA NOT NULL,
          created_at TEXT NOT NULL, PRIMARY KEY(job_id,version_number),
          FOREIGN KEY(job_id) REFERENCES jobs(job_id)
        );
        """
    else:
        schema = """
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS jobs (
          job_id TEXT PRIMARY KEY, source BLOB NOT NULL, payload TEXT NOT NULL,
          profile TEXT, candidate BLOB, operations TEXT NOT NULL DEFAULT '[]',
          reviews TEXT NOT NULL DEFAULT '[]', artifact BLOB
        );
        CREATE TABLE IF NOT EXISTS audit_events (
          event_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
          event_type TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(job_id) REFERENCES jobs(job_id)
        );
        CREATE INDEX IF NOT EXISTS idx_audit_job ON audit_events(job_id, event_id);
        CREATE TABLE IF NOT EXISTS evaluation_runs (
          run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, status TEXT NOT NULL,
          summary TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dataset_versions (
          job_id TEXT NOT NULL, version_number INTEGER NOT NULL,
          operation_id TEXT, content_hash TEXT NOT NULL, artifact BLOB NOT NULL,
          created_at TEXT NOT NULL, PRIMARY KEY(job_id,version_number),
          FOREIGN KEY(job_id) REFERENCES jobs(job_id)
        );
        """
    with connect(path) as database:
        database.executescript(schema)


def insert_job(path: str, job: dict[str, Any], source: bytes) -> None:
    with connect(path) as database:
        database.execute("INSERT INTO jobs(job_id,source,payload) VALUES(?,?,?)",
                         (job["job_id"], source, json.dumps(job)))
        database.execute("INSERT INTO audit_events(job_id,event_type,details,created_at) VALUES(?,?,?,?)",
                         (job["job_id"], "uploaded", "{}", job["created_at"]))


def get_job(path: str, job_id: str) -> dict[str, Any] | None:
    with connect(path) as database:
        row = database.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["payload"] = json.loads(result["payload"])
    result["profile"] = json.loads(result["profile"]) if result["profile"] else None
    result["operations"] = json.loads(result["operations"])
    result["reviews"] = json.loads(result["reviews"])
    return result


def update_job(path: str, job_id: str, *, payload: dict[str, Any] | None = None,
               profile: dict[str, Any] | None = None, candidate: bytes | None = None,
               operations: list[dict[str, Any]] | None = None,
               reviews: list[dict[str, Any]] | None = None,
               artifact: bytes | None = None, event: tuple[str, dict[str, Any]] | None = None) -> None:
    fields, values = [], []
    for key, value in (("payload", json.dumps(payload) if payload is not None else None),
                       ("profile", json.dumps(profile) if profile is not None else None),
                       ("candidate", candidate),
                       ("operations", json.dumps(operations) if operations is not None else None),
                       ("reviews", json.dumps(reviews) if reviews is not None else None),
                       ("artifact", artifact)):
        if value is not None:
            fields.append(f"{key}=?")
            values.append(value)
    if fields:
        with connect(path) as database:
            database.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE job_id=?", (*values, job_id))
            if event:
                from datetime import datetime, timezone
                database.execute("INSERT INTO audit_events(job_id,event_type,details,created_at) VALUES(?,?,?,?)",
                    (job_id, event[0], json.dumps(event[1]), datetime.now(timezone.utc).isoformat()))


def events(path: str, job_id: str) -> list[dict[str, Any]]:
    with connect(path) as database:
        rows = database.execute("SELECT event_type,details,created_at FROM audit_events WHERE job_id=? ORDER BY event_id", (job_id,)).fetchall()
    return [{"event_type": r["event_type"], "details": json.loads(r["details"]), "created_at": r["created_at"]} for r in rows]


def list_jobs(path: str, limit: int = 25) -> list[dict[str, Any]]:
    with connect(path) as database:
        rows = database.execute("SELECT payload FROM jobs").fetchall()
    jobs = [json.loads(row[0]) for row in rows]
    return sorted(jobs, key=lambda job: job.get("created_at", ""), reverse=True)[:limit]


def job_counts(path: str) -> dict[str, int]:
    with connect(path) as database:
        rows = database.execute("SELECT payload FROM jobs").fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        status = json.loads(row[0]).get("status", "UNKNOWN")
        counts[status] = counts.get(status, 0) + 1
    return {"total": sum(counts.values()), "completed": counts.get("COMPLETED", 0),
            "review": counts.get("NEEDS_REVIEW", 0), "failed": counts.get("FAILED", 0)}


def save_evaluation(path: str, summary: dict[str, Any]) -> str:
    import uuid
    run_id = str(uuid.uuid4())
    from datetime import datetime, timezone
    created = summary.get("generated_at") or datetime.now(timezone.utc).isoformat()
    with connect(path) as database:
        database.execute("INSERT INTO evaluation_runs(run_id,mode,status,summary,created_at) VALUES(?,?,?,?,?)",
            (run_id, summary.get("mode", "unknown"), summary.get("status", "unknown"), json.dumps(summary), created))
    return run_id


def list_evaluations(path: str, limit: int = 20) -> list[dict[str, Any]]:
    with connect(path) as database:
        rows = database.execute("SELECT run_id,mode,status,summary,created_at FROM evaluation_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [{"run_id": row[0], "mode": row[1], "status": row[2], "summary": json.loads(row[3]), "created_at": row[4]} for row in rows]


def store_dataset_version(path: str, job_id: str, version_number: int, artifact: bytes,
                          operation_id: str | None = None) -> None:
    import hashlib
    from datetime import datetime, timezone
    digest = hashlib.sha256(artifact).hexdigest()
    created = datetime.now(timezone.utc).isoformat()
    query = "INSERT INTO dataset_versions(job_id,version_number,operation_id,content_hash,artifact,created_at) VALUES(?,?,?,?,?,?)"
    if _is_postgres(path):
        query += " ON CONFLICT(job_id,version_number) DO NOTHING"
    else:
        query = query.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1)
    with connect(path) as database:
        database.execute(query, (job_id, version_number, operation_id, digest, artifact, created))


def list_dataset_versions(path: str, job_id: str) -> list[dict[str, Any]]:
    with connect(path) as database:
        rows = database.execute("SELECT version_number,operation_id,content_hash,created_at FROM dataset_versions WHERE job_id=? ORDER BY version_number", (job_id,)).fetchall()
    return [{"version_number": r[0], "operation_id": r[1], "content_hash": r[2], "created_at": r[3]} for r in rows]
