from __future__ import annotations

import io
import json
import zipfile
import secrets
import hmac

import pandas as pd

from flask import abort, current_app, flash, redirect, render_template, request, send_file, session, url_for

from app.routes import bp
from app.services.jobs import apply_yaml_spec, candidate_frame, cell_provenance, create_job, decide_review, finalize_job, get_job, replay_job
from app.profiling.profiler import profile_dataset
from app.persistence.db import events, job_counts, list_evaluations, list_jobs, save_evaluation
from app.ingestion.readers import inspect_excel


@bp.app_context_processor
def inject_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return {"csrf_token": session["csrf_token"]}


@bp.before_request
def verify_csrf():
    if request.method == "POST" and request.endpoint != "main.sheets":
        expected = session.get("csrf_token", "")
        submitted = request.form.get("csrf_token", "")
        if not expected or not submitted or not secrets.compare_digest(expected, submitted):
            abort(400, "Invalid form token")


@bp.before_request
def require_login():
    if not current_app.config.get("ACCESS_PASSWORD"):
        return None
    if request.endpoint in {"main.login", "main.css_asset", "static"}:
        return None
    if not session.get("authenticated"):
        return redirect(url_for("main.login", next=request.path))
    return None


def _database() -> str:
    return current_app.config.get("DATABASE_URL") or current_app.config["DATABASE_PATH"]


@bp.route("/login", methods=["GET", "POST"])
def login():
    if not current_app.config.get("ACCESS_PASSWORD"):
        return redirect(url_for("main.index"))
    if request.method == "POST":
        supplied = request.form.get("password", "")
        if hmac.compare_digest(supplied, current_app.config["ACCESS_PASSWORD"]):
            session.clear()
            session["authenticated"] = True
            session.permanent = True
            return redirect(url_for("main.index"))
        flash("Incorrect password.", "error")
    return render_template("login.html")


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.login"))


@bp.get("/css/<path:filename>")
def css_asset(filename: str):
    from flask import send_from_directory
    from pathlib import Path
    return send_from_directory(Path(__file__).resolve().parents[2] / "public" / "css", filename)


@bp.get("/")
def index():
    jobs = list_jobs(_database())
    return render_template("index.html", jobs=jobs, counts=job_counts(_database()))


@bp.post("/sheets")
def sheets():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename or not uploaded.filename.lower().endswith(".xlsx"):
        return {"error": "Choose an XLSX workbook."}, 400
    try:
        return {"sheets": inspect_excel(uploaded.read(), current_app.config["MAX_SHEETS"])}
    except ValueError as exc:
        return {"error": str(exc)}, 400


@bp.post("/jobs")
def upload_job():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        flash("Choose a CSV or XLSX file to continue.", "error")
        return redirect(url_for("main.index"))
    try:
        job = create_job(_database(), uploaded.read(), uploaded.filename,
            request.form.get("objective", "Prepare for analysis"), request.form.get("sheet") or None,
            current_app.config["MAX_ROWS"], current_app.config["MAX_COLUMNS"], current_app.config["MAX_AGENT_STEPS"],
            current_app.config["MAX_SHEETS"], current_app.config["MAX_OPERATIONS"])
    except Exception as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.index"))
    return redirect(url_for("main.job_detail", job_id=job["payload"]["job_id"]))


@bp.get("/jobs/<job_id>")
def job_detail(job_id: str):
    job = get_job(_database(), job_id)
    if not job:
        return "Job not found", 404
    return render_template("job.html", job=job, events=events(_database(), job_id))


@bp.post("/jobs/<job_id>/reviews/<review_id>")
def review(job_id: str, review_id: str):
    try:
        job = decide_review(_database(), job_id, review_id, request.form.get("decision", ""),
            request.form.get("reason", ""), current_app.config["MAX_ROWS"], current_app.config["MAX_COLUMNS"],
            current_app.config["MAX_AGENT_STEPS"], request.form.get("arguments_json"), request.form.get("columns_json"))
        flash(f"Review recorded. Job status: {job['payload']['status']}.", "success")
    except (ValueError, KeyError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.job_detail", job_id=job_id))


@bp.post("/jobs/<job_id>/finalize")
def finalize(job_id: str):
    try:
        finalize_job(_database(), job_id)
        flash("Dataset finalized and ready to download.", "success")
    except (ValueError, KeyError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.job_detail", job_id=job_id))


@bp.post("/jobs/<job_id>/spec")
def apply_spec(job_id: str):
    try:
        job = apply_yaml_spec(_database(), job_id, request.form.get("yaml_spec", ""), current_app.config["MAX_AGENT_STEPS"])
        flash(f"Specification processed. Job status: {job['payload']['status']}.", "success")
    except (ValueError, KeyError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.job_detail", job_id=job_id))


@bp.get("/jobs/<job_id>/download/<kind>")
def download(job_id: str, kind: str):
    job = get_job(_database(), job_id)
    if not job:
        return "Job not found", 404
    if kind == "source":
        data, name = job["source"], f"{job['payload']['job_id']}-source.{job['payload']['source_format']}"
    elif kind == "cleaned" and job["payload"]["status"] == "COMPLETED" and job["artifact"]:
        data, name = job["artifact"], f"{job['payload']['job_id']}-cleaned.csv"
    elif kind == "cleaned-xlsx" and job["payload"]["status"] == "COMPLETED" and job["artifact"]:
        frame = pd.read_csv(io.BytesIO(job["artifact"]))
        stream = io.BytesIO()
        with pd.ExcelWriter(stream, engine="openpyxl") as writer:
            frame.to_excel(writer, index=False, sheet_name="Prepared data")
        data, name = stream.getvalue(), f"{job['payload']['job_id']}-cleaned.xlsx"
    else:
        return "Artifact not found", 404
    mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if name.endswith(".xlsx") else "text/csv"
    return send_file(io.BytesIO(data), as_attachment=True, download_name=name, mimetype=mimetype)


@bp.get("/jobs/<job_id>/quality-report")
def quality_report(job_id: str):
    job = get_job(_database(), job_id)
    if not job:
        return "Job not found", 404
    current = candidate_frame(job)
    return {"source": job["profile"], "candidate": profile_dataset(current.drop(columns=["_source_row_id"], errors="ignore")),
            "ledger": job["operations"], "validation": job["payload"].get("metadata", {}).get("validation"),
            "job_status": job["payload"]["status"]}


@bp.get("/jobs/<job_id>/ledger.json")
def ledger_download(job_id: str):
    job = get_job(_database(), job_id)
    if not job:
        return "Job not found", 404
    data = json.dumps(job["operations"], indent=2).encode("utf-8")
    return send_file(io.BytesIO(data), as_attachment=True,
        download_name=f"{job_id}-operation-ledger.json", mimetype="application/json")


@bp.get("/jobs/<job_id>/lineage")
def lineage(job_id: str):
    job = get_job(_database(), job_id)
    if not job:
        return "Job not found", 404
    row_id = request.args.get("row_id", "")
    column = request.args.get("column", "")
    if not row_id or not column:
        return {"error": "Provide row_id and column query parameters."}, 400
    return cell_provenance(job, row_id, column)


@bp.get("/jobs/<job_id>/replay")
def replay(job_id: str):
    job = get_job(_database(), job_id)
    if not job:
        return "Job not found", 404
    try:
        frame = replay_job(_database(), job_id)
        from app.services.jobs import _df_bytes
        import hashlib
        replay_hash = hashlib.sha256(_df_bytes(frame)).hexdigest()
        current_hash = hashlib.sha256(job["candidate"] or b"").hexdigest()
        return {"status": "MATCH" if replay_hash == current_hash else "DIVERGED",
                "replayed_rows": len(frame), "replay_hash": replay_hash,
                "candidate_hash": current_hash, "source_hash": job["payload"]["source_hash"]}
    except ValueError as exc:
        return {"status": "REPLAY_MISMATCH", "reason": str(exc)}, 409


@bp.get("/jobs/<job_id>/audit.zip")
def audit_package(job_id: str):
    job = get_job(_database(), job_id)
    if not job:
        return "Job not found", 404
    if job["payload"]["status"] != "COMPLETED" or not job["artifact"]:
        return "Job must be finalized before exporting its audit package.", 409
    current = candidate_frame(job)
    report = {"source": job["profile"], "candidate": profile_dataset(current.drop(columns=["_source_row_id"], errors="ignore")),
              "validation": job["payload"].get("metadata", {}).get("validation"),
              "job": job["payload"], "events": events(_database(), job_id)}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("cleaned.csv", job["artifact"])
        archive.writestr("operation-ledger.json", json.dumps(job["operations"], indent=2))
        archive.writestr("quality-report.json", json.dumps(report, indent=2))
        archive.writestr("replay-metadata.json", json.dumps({"source_hash": job["payload"]["source_hash"],
            "candidate_hash": __import__("hashlib").sha256(job["candidate"]).hexdigest()}, indent=2))
        spec = job["payload"].get("metadata", {}).get("specification")
        if spec:
            archive.writestr("preparation-specification.json", json.dumps(spec, indent=2))
    return send_file(io.BytesIO(buffer.getvalue()), as_attachment=True,
        download_name=f"{job_id}-audit.zip", mimetype="application/zip")


@bp.get("/evaluation")
def evaluation_lab():
    return render_template("evaluation.html", runs=list_evaluations(_database()))


@bp.post("/evaluation/run")
def run_evaluation():
    from app.evaluation.runner import run
    summary = run(request.form.get("mode", "deterministic"), count=100)
    save_evaluation(_database(), summary)
    flash(f"Evaluation {summary['status'].lower()}: {summary['cases']} cases.", "success")
    return redirect(url_for("main.evaluation_lab"))
