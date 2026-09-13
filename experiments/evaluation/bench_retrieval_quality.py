#!/usr/bin/env python3
"""Arm-C retrieval-quality re-test on Real ONNX vs Shim.

Reuses stage11_12/runner.run_arm("C", cases) with the gateway swapped to the
env-driven real path (KYLIN_USE_SHIM=0 + KYLIN_EMBEDDING_MODEL).  Output keeps
the stage11.report.json arms["C"] metric structure (METRIC_LABELS keys) and
adds a Shim-vs-Real delta so structure drift is visible.

Expected (verified on the Shim baseline + Real re-test): eligible_recall /
knowledge_precision unchanged, hard_violation / forget_leakage / stale_reuse
stay 0 — absolute similarity scores change but threshold=0 makes ER robust.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2] / "eagle_os_agent"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stage11_12"))

import runner  # noqa: E402

from eagle.bootstrap import create_auto_gateway  # noqa: E402

# headline safety-first metrics (short keys, same ordering/scope as stage 11)
HEADLINE = [
    "hard_violation",
    "forget_leakage",
    "pref_false_promotion",
    "stale_reuse",
    "conflict_resolved",
    "knowledge_precision",
    "knowledge_recall",
    "pref_precision",
    "traceability",
    "task_success",
    "eligible_recall",
]

_state = {"n": 0, "backend": None}


def _real_gateway(collection: str):
    """Env-driven gateway with a UNIQUE engine collection per case, so the
    persistent real engine never sees cross-case stale rows (user_ids repeat
    across the 200 GOV cases)."""
    _state["n"] += 1
    gw = create_auto_gateway(
        history_db_path=runner.tempfile_path(collection),
        collection_name=f"eagle_eval_{os.getpid()}_{_state['n']}",
    )
    _state["backend"] = gw.backend
    return gw


def _run_and_aggregate(cases: list[dict]) -> dict:
    scored = runner.run_arm("C", cases)
    return runner.aggregate_metrics(scored)


def _metrics_block(metrics: dict) -> dict:
    return {runner.METRIC_LABELS[k]: metrics.get(k) for k in HEADLINE}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--total", type=int, default=200)
    parser.add_argument("--shim-only", action="store_true", help="run arm C on shim only")
    parser.add_argument("--real-only", action="store_true", help="run arm C on real path only")
    parser.add_argument(
        "--out", type=Path, default=Path(__file__).with_name("bench_retrieval_quality.json")
    )
    args = parser.parse_args()

    cases = [case.to_json() for case in runner._generate.generate(args.seed, args.total)]
    report = {"seed": args.seed, "total": len(cases), "arm": "C"}

    if not args.real_only:
        report["arms"] = {"C_shim": _metrics_block(_run_and_aggregate(cases))}

    if not args.shim_only:
        orig_gateway = runner._gateway
        runner._gateway = _real_gateway
        try:
            real_metrics = _run_and_aggregate(cases)
        finally:
            runner._gateway = orig_gateway
        report["arms"]["C_real"] = _metrics_block(real_metrics)
        report["real_backend"] = _state["backend"]

    # delta: Real - Shim for the headline metrics (None-safe)
    shim_block = report.get("arms", {}).get("C_shim")
    real_block = report.get("arms", {}).get("C_real")
    if shim_block is not None and real_block is not None:
        report["delta_real_minus_shim"] = {
            label: (
                round(real_block[label] - shim_block[label], 4)
                if shim_block[label] is not None and real_block[label] is not None
                else None
            )
            for label in shim_block
        }

    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
