import io

from app.services.jobs import apply_yaml_spec, create_job, decide_review, finalize_job, get_job, replay_job
from app.persistence.db import connect, list_dataset_versions


def _csrf(client):
    client.get("/")
    with client.session_transaction() as session:
        return session["csrf_token"]


def test_upload_profile_review_and_download(app, client):
    token = _csrf(client)
    response = client.post("/jobs", data={
        "csrf_token": token, "objective": "Prepare names",
        "file": (io.BytesIO(b"name,amount\n Alice ,2\n"), "customers.csv")
    }, content_type="multipart/form-data", follow_redirects=False)
    assert response.status_code == 302
    job_path = response.headers["Location"]
    page = client.get(job_path)
    assert page.status_code == 200
    assert b"Quality profile" in page.data
    assert b"READY_TO_FINALIZE" in page.data
    job_id = job_path.rstrip("/").split("/")[-1]
    job = get_job(app.config["DATABASE_URL"] or app.config["DATABASE_PATH"], job_id)
    finalized = client.post(job_path + "/finalize", data={"csrf_token": token}, follow_redirects=True)
    assert b"Download cleaned CSV" in finalized.data
    download = client.get(job_path + "/download/cleaned")
    assert download.status_code == 200
    assert b"Alice" in download.data


def test_upload_rejects_unsupported_file(client):
    token = _csrf(client)
    response = client.post("/jobs", data={"csrf_token": token,
        "file": (io.BytesIO(b"not really a pdf"), "unsafe.pdf")}, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    assert b"Only CSV and XLSX" in response.data


def test_csrf_blocks_state_change(client):
    response = client.post("/jobs/fake/finalize", data={})
    assert response.status_code == 400


def test_reviewed_dedupe_replay_and_lineage(app, client):
    database = app.config["DATABASE_PATH"]
    job = create_job(database, b"name\nAda\nAda\n", "dup.csv", "dedupe", None, 100, 20, 5)
    assert job["payload"]["status"] == "NEEDS_REVIEW"
    review = next(r for r in job["reviews"] if r["status"] == "OPEN")
    import pytest
    with pytest.raises(ValueError, match="prohibited"):
        decide_review(database, job["payload"]["job_id"], review["review_id"], "APPROVE", "exact duplicates", 100, 20)
    unchanged = get_job(database, job["payload"]["job_id"])
    assert len(unchanged["candidate"].decode().splitlines()) == 3
    resolved = decide_review(database, job["payload"]["job_id"], review["review_id"], "APPROVE",
        "exact duplicates", 100, 20, allow_row_deletion=True)
    assert resolved["payload"]["status"] == "READY_TO_FINALIZE"
    assert len(resolved["operations"]) == 1
    assert resolved["operations"][0]["status"] == "EXECUTED"
    assert resolved["operations"][0]["evidence"]["removed_source_row_ids"]
    row_id = resolved["candidate"].decode().splitlines()[1].split(",")[0]
    lineage = client.get(f"/jobs/{job['payload']['job_id']}/lineage?row_id={row_id}&column=name")
    assert lineage.status_code == 200
    finalized = finalize_job(database, job["payload"]["job_id"])
    assert finalized["payload"]["status"] == "COMPLETED"
    replayed = replay_job(database, job["payload"]["job_id"])
    assert len(replayed) == 1
    versions = list_dataset_versions(database, job["payload"]["job_id"])
    assert [version["version_number"] for version in versions] == [0, 1]
    replay_response = client.get(f"/jobs/{job['payload']['job_id']}/replay")
    assert replay_response.json["status"] == "MATCH"
    audit = client.get(f"/jobs/{job['payload']['job_id']}/audit.zip")
    assert audit.status_code == 200


def test_replay_detects_source_hash_mismatch(app):
    database = app.config["DATABASE_PATH"]
    job = create_job(database, b"name\nAda\n", "source.csv", "prepare", None, 100, 20, 5)
    with connect(database) as connection:
        connection.execute("UPDATE jobs SET source=? WHERE job_id=?", (b"tampered", job["payload"]["job_id"]))
    import pytest
    with pytest.raises(ValueError, match="source hash"):
        replay_job(database, job["payload"]["job_id"])


def test_formula_injection_is_sanitized_at_export(app):
    database = app.config["DATABASE_PATH"]
    job = create_job(database, b"value\n=2+2\n", "formula.csv", "prepare", None, 100, 20, 5)
    done = finalize_job(database, job["payload"]["job_id"])
    assert done["artifact"].decode().splitlines()[1] == "'=2+2"


def test_yaml_critical_rule_blocks_finalization(app):
    from app.services.jobs import apply_yaml_spec
    database = app.config["DATABASE_PATH"]
    job = create_job(database, b"amount\n-2\n", "amount.csv", "prepare", None, 100, 20, 5)
    applied = apply_yaml_spec(database, job["payload"]["job_id"], '''version: "1.0"
dataset: {objective: validate}
schema:
  strict: false
  columns:
    amount: {type: decimal, nullable: false, minimum: 0}
operations: []
''', 5)
    assert applied["payload"]["metadata"]["validation"]["passed"] is False
    assert applied["payload"]["status"] == "READY_TO_FINALIZE"
    import pytest
    with pytest.raises(ValueError, match="quality gate"):
        finalize_job(database, job["payload"]["job_id"])
    assert get_job(database, job["payload"]["job_id"])["payload"]["status"] == "FAILED"
