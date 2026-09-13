"""Governance-tax analysis: decompose safety blocks from unsafe execution.

The two-tier arm keeps the executable PACK strict and exposes only non-executable
hints from non-ACTIVE knowledge. Hints cannot select tools; they are an input for
clarification/revalidation, so safety metrics must remain unchanged.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "eagle_os_agent"))
sys.path.insert(0, str(ROOT / "stage11_12"))

import runner as stage  # noqa: E402


def summarize(scored):
    metrics = stage.aggregate_metrics(scored)
    return {
        key: metrics.get(key)
        for key in (
            "eligible_recall",
            "safe_task_success",
            "unsafe_execution",
            "missed_safe_action",
            "correct_policy_block",
            "governed_visibility",
            "knowledge_recall",
            "task_success",
            "hint_assisted_safe_task_success",
            "hint_triggered_revalidation",
            "hint_induced_unsafe_execution",
        )
    }


def main():
    seeds = [42, 43, 44]
    total = 200
    results = {"conditions": {"seeds": seeds, "total_per_seed": total, "top_k": 5}, "arms": {}}
    for mode, two_tier, safe_fallback in (
        ("strict", False, False),
        ("two-tier-hints", True, False),
        ("two-tier-safe-fallback", True, True),
    ):
        per_seed = []
        for seed in seeds:
            cases = [case.to_json() for case in stage._generate.generate(seed, total)]
            scored = stage.run_arm(
                "C",
                cases,
                two_tier=two_tier,
                safe_fallback=safe_fallback,
            )
            metrics = summarize(scored)
            per_seed.append(metrics)
            print(f"[{mode} seed={seed}] {json.dumps(metrics, ensure_ascii=False)}")
        results["arms"][mode] = per_seed
    output = ROOT / "governance_tax.report.json"
    output.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
    print(f"report: {output}")


if __name__ == "__main__":
    main()
