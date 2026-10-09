from __future__ import annotations

import argparse
import json
import time
import os
from datetime import datetime, timezone
from pathlib import Path

from app.agent.planner import EvidencePlanner
from app.agent.graph import run_workflow
from app.evaluation.baselines import fixed_pandas_baseline, noop_baseline
from app.evaluation.generators import generate_cases
from app.evaluation.metrics import changed_cell_counts, exact_cell_accuracy


def run(mode: str = "deterministic", count: int = 100, seed: int = 4242) -> dict:
    import os
    planner = EvidencePlanner()
    hosted_client = None
    if mode == "llm":
        if not os.environ.get("OPENAI_API_KEY"):
            return {"mode": "llm", "status": "SKIPPED", "reason": "OPENAI_API_KEY is not configured; no LLM cases were run.", "cases": 0}
        if os.environ.get("PREPPILOT_ALLOW_DATA_TO_LLM", "0").lower() not in {"1", "true", "yes"}:
            return {"mode": "llm", "status": "SKIPPED", "reason": "PREPPILOT_ALLOW_DATA_TO_LLM is not enabled; no profile data was sent.", "cases": 0}
        try:
            from app.agent.llm_client import OpenAIPlannerClient
            from app.agent.planner import HostedPlanner
            hosted_client = OpenAIPlannerClient()
            planner = HostedPlanner(hosted_client)
        except Exception as exc:
            return {"mode": "llm", "status": "FAILED", "reason": str(exc), "cases": 0}
    cases = generate_cases(count=count, seed=seed)
    results = []
    failures = 0
    operations_total = 0
    reviews_total = 0
    started = time.perf_counter()
    for case in cases:
        source = case.frame
        noop = noop_baseline(source)
        baseline = fixed_pandas_baseline(source)
        try:
            agent_result = run_workflow(source, f"Prepare {case.family} scenario", planner, max_steps=5)
            agent = agent_result["frame"]
        except Exception as exc:
            failures += 1
            agent_result = {"frame": source.copy(deep=True), "status": "FAILED", "ledger": [],
                            "review_required": False, "error": type(exc).__name__}
            agent = agent_result["frame"]
        operations_total += len(agent_result.get("ledger", []))
        reviews_total += int(bool(agent_result.get("review_required")))
        results.append({"case_id": case.case_id, "family": case.family,
            "expected_action": case.expected_action, "agent_status": agent_result.get("status"),
            "noop_exact_accuracy": exact_cell_accuracy(noop, case.expected),
            "baseline_exact_accuracy": exact_cell_accuracy(baseline, case.expected),
            "agent_exact_accuracy": exact_cell_accuracy(agent, case.expected),
            "noop_changes": changed_cell_counts(source, noop, case.expected),
            "baseline_changes": changed_cell_counts(source, baseline, case.expected),
            "agent_changes": changed_cell_counts(source, agent, case.expected),
            "agent_operations": len(agent_result.get("ledger", [])),
            "review_required": bool(agent_result.get("review_required"))})
    duration = time.perf_counter() - started
    summary = {"mode": mode, "status": "COMPLETED", "seed": seed, "cases": len(results),
        "scenario_families": sorted({r["family"] for r in results}), "duration_seconds": round(duration, 4),
        "mean_exact_accuracy": {key: round(sum(r[key] for r in results) / max(1, len(results)), 6)
            for key in ["noop_exact_accuracy", "baseline_exact_accuracy", "agent_exact_accuracy"]},
        "human_review_cases": reviews_total, "failed_runs": failures,
        "average_operations": operations_total / max(1, len(results)),
        "planner_model": hosted_client.model if hosted_client else None,
        "token_usage": hosted_client.last_usage if hosted_client else None,
        "generated_at": datetime.now(timezone.utc).isoformat(), "case_results": results}
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run reproducible PrepPilot benchmark cases")
    parser.add_argument("--mode", choices=["deterministic", "scripted-agent", "llm"], default="deterministic")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--seed", type=int, default=4242)
    args = parser.parse_args()
    report = run(args.mode, args.count, args.seed)
    output_dir = Path("data/benchmarks")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "latest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    table = "\n".join(f"- {k}: {v}" for k, v in report.get("mean_exact_accuracy", {}).items())
    markdown = f"# PrepPilot Benchmark Report\n\nGenerated: {report.get('generated_at', 'not run')}\n\nStatus: {report['status']}\n\nCases: {report['cases']}\n\nSeed: {report.get('seed', 'n/a')}\n\nMean exact cell accuracy:\n{table}\n\nThis report is generated from executable synthetic scenarios. It is not evidence of performance on all real datasets.\n"
    (output_dir / "latest.md").write_text(markdown, encoding="utf-8")
    from app.persistence.db import initialize, save_evaluation
    database = os.environ.get("DATABASE_URL") or os.environ.get("PREPPILOT_DATABASE_PATH", "instance/preppilot.sqlite3")
    initialize(database)
    run_id = save_evaluation(database, report)
    report["run_id"] = run_id
    print(json.dumps({k: v for k, v in report.items() if k != "case_results"}, indent=2))


if __name__ == "__main__":
    main()
