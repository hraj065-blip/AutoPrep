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
    if count < 1:
        raise ValueError("count must be at least 1")
    import os
    planner = EvidencePlanner()
    hosted_client = None
    if mode == "llm":
        if os.environ.get("PREPPILOT_ALLOW_DATA_TO_LLM", "0").lower() not in {"1", "true", "yes"}:
            return {"mode": "llm", "status": "SKIPPED", "reason": "PREPPILOT_ALLOW_DATA_TO_LLM is not enabled; no profile data was sent.", "cases": 0}
        try:
            from app.agent.planner import configured_planner
            planner, hosted_client = configured_planner()
            if hosted_client is None:
                return {"mode": "llm", "status": "SKIPPED", "reason": "Set PREPPILOT_PLANNER to groq or openai to run hosted LLM cases.", "cases": 0}
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
            agent_result = {"frame": source.copy(deep=True), "status": "FAILED", "ledger": [],
                            "review_required": False, "error": type(exc).__name__}
            agent = agent_result["frame"]
        if agent_result.get("status") in {"FAILED", "SAFE_STOP"}:
            failures += 1
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
            "expected_review": case.family == "duplicate",
            "review_required": bool(agent_result.get("review_required"))})
    duration = time.perf_counter() - started
    strategies = {
        "noop": ("noop_exact_accuracy", "noop_changes"),
        "fixed_pandas": ("baseline_exact_accuracy", "baseline_changes"),
        "agent": ("agent_exact_accuracy", "agent_changes"),
    }

    def aggregate(group: list[dict]) -> dict:
        count_group = max(1, len(group))
        scores = {}
        for name, (accuracy_key, changes_key) in strategies.items():
            scores[name] = {
                "mean_exact_cell_accuracy": round(sum(row[accuracy_key] for row in group) / count_group, 6),
                "changed_cell_precision": round(
                    sum(row[changes_key]["changed_cell_precision"] for row in group) / count_group, 6),
                "changed_cell_recall": round(
                    sum(row[changes_key]["changed_cell_recall"] for row in group) / count_group, 6),
                "incorrect_change_rate": round(
                    sum(row[changes_key]["incorrect_change_rate"] for row in group) / count_group, 6),
            }
        expected_reviews = sum(bool(row["expected_review"]) for row in group)
        predicted_reviews = sum(bool(row["review_required"]) for row in group)
        true_reviews = sum(bool(row["expected_review"]) and bool(row["review_required"]) for row in group)
        scores["agent"]["review_precision"] = (
            round(true_reviews / predicted_reviews, 6) if predicted_reviews
            else (1.0 if not expected_reviews else 0.0))
        scores["agent"]["review_recall"] = round(true_reviews / expected_reviews, 6) if expected_reviews else 1.0
        return scores

    by_family = {family: aggregate([row for row in results if row["family"] == family])
                 for family in sorted({row["family"] for row in results})}
    summary = {"mode": mode, "status": "COMPLETED", "seed": seed, "cases": len(results),
        "scenario_families": sorted({r["family"] for r in results}), "duration_seconds": round(duration, 4),
        "mean_exact_accuracy": {key: round(sum(r[key] for r in results) / max(1, len(results)), 6)
            for key in ["noop_exact_accuracy", "baseline_exact_accuracy", "agent_exact_accuracy"]},
        "human_review_cases": reviews_total, "failed_runs": failures,
        "average_operations": operations_total / max(1, len(results)),
        "aggregate_metrics": aggregate(results), "by_family": by_family,
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
    aggregate = report.get("aggregate_metrics", {})
    table = "\n".join(
        f"- {strategy}: exact={metrics['mean_exact_cell_accuracy']:.4f}, "
        f"changed-cell precision={metrics['changed_cell_precision']:.4f}, "
        f"recall={metrics['changed_cell_recall']:.4f}, "
        f"incorrect-change rate={metrics['incorrect_change_rate']:.4f}"
        for strategy, metrics in aggregate.items())
    family_table = "\n".join(
        f"- {family}: agent exact={metrics['agent']['mean_exact_cell_accuracy']:.4f}, "
        f"review precision={metrics['agent']['review_precision']:.4f}, "
        f"review recall={metrics['agent']['review_recall']:.4f}"
        for family, metrics in report.get("by_family", {}).items())
    markdown = (f"# PrepPilot Benchmark Report\n\nGenerated: {report.get('generated_at', 'not run')}\n\n"
        f"Status: {report['status']}\n\nCases: {report['cases']}\n\nSeed: {report.get('seed', 'n/a')}\n\n"
        f"Aggregate metrics (macro-average by case):\n{table}\n\nPer-family agent accuracy and review routing:\n{family_table}\n\n"
        f"Agent failures: {report.get('failed_runs', 0)}\n\n"
        "This report is generated from executable synthetic scenarios. It is not evidence of performance on all real datasets.\n")
    (output_dir / "latest.md").write_text(markdown, encoding="utf-8")
    from app.persistence.db import initialize, save_evaluation
    database = os.environ.get("DATABASE_URL") or os.environ.get("PREPPILOT_DATABASE_PATH", "instance/preppilot.sqlite3")
    initialize(database)
    run_id = save_evaluation(database, report)
    report["run_id"] = run_id
    print(json.dumps({k: v for k, v in report.items() if k != "case_results"}, indent=2))


if __name__ == "__main__":
    main()
