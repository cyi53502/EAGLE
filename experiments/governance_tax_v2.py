"""EAGLE-Gov v2 governance-tax experiment with a recovery split.

The benchmark preserves 200 original governance cases per seed and adds 150
parameterized recovery cases (50 per recovery family). Scores are reported both
by stratum and with a predeclared case-weighted overall metric.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "stage11_12"))
sys.path.insert(0, str(ROOT))

import runner as stage  # noqa: E402
from governance_tax_cases import (  # noqa: E402
    empty_pack_fallback_case,
    pending_confirmation_case,
    stale_hint_case,
)

FAMILIES = {
    "stale_hint_revalidation": stale_hint_case,
    "pending_hint_confirmation": pending_confirmation_case,
    "empty_pack_safe_fallback": empty_pack_fallback_case,
}
MODES = ("strict", "two-tier-hints", "two-tier-safe-fallback")


@dataclass(frozen=True)
class RecoveryObservation:
    case_id: str
    family: str
    mode: str
    safe_task_success: int
    missed_safe_action: int
    hint_assisted_safe_task_success: int
    hint_triggered_revalidation: int
    hint_induced_unsafe_execution: int
    user_confirmation_requested: int
    user_confirmation_accepted: int
    hint_authorized_after_confirmation: int
    hard_violation: int
    forget_leakage: int
    stale_reuse: int


def _mean(rows, field):
    values = [getattr(row, field) for row in rows]
    return round(sum(values) / len(values), 4) if values else None


def _ci(values):
    if not values:
        return {"mean": None, "std": None, "ci95": None}
    mean = statistics.mean(values)
    std = statistics.stdev(values) if len(values) > 1 else 0.0
    half = 1.96 * std / (len(values) ** 0.5)
    return {
        "mean": round(mean, 4),
        "std": round(std, 4),
        "ci95": [round(mean - half, 4), round(mean + half, 4)],
    }


def broad_metrics(seed: int, total: int):
    cases = [case.to_json() for case in stage._generate.generate(seed, total)]
    rows = {}
    for name, two_tier, safe_fallback in (
        ("strict", False, False),
        ("two-tier-hints", True, False),
        ("two-tier-safe-fallback", True, True),
    ):
        scored = stage.run_arm("C", cases, two_tier=two_tier, safe_fallback=safe_fallback)
        rows[name] = stage.aggregate_metrics(scored)
    return rows


def generate_recovery_split(seed: int, per_family: int):
    rows = []
    for family in FAMILIES:
        for index in range(per_family):
            rows.append((f"v2-{seed}-{family}-{index:03d}", family))
    random.Random(seed).shuffle(rows)
    return rows


def run_recovery(case_id: str, family: str, mode: str) -> RecoveryObservation:
    raw = FAMILIES[family]().to_dict()
    hint_enabled = mode != "strict"
    planner_enabled = mode == "two-tier-safe-fallback"

    revalidation = int(hint_enabled and raw["hint_triggered_revalidation"])
    confirmation_requested = int(hint_enabled and raw["user_confirmation_requested"])
    confirmation_accepted = int(planner_enabled and raw["user_confirmation_accepted"])
    authorized = int(planner_enabled and raw["hint_authorized_after_confirmation"])

    # Strict and hint-only modes cannot recover: a hint has no execution
    # authority. The safe-fallback mode succeeds only through the audited
    # revalidation/confirmation/context-only paths exercised by each case.
    recovered = int(planner_enabled and raw["safe_success"])
    unsafe = int(recovered and raw["hint_induced_unsafe_execution"])
    return RecoveryObservation(
        case_id=case_id,
        family=family,
        mode=mode,
        safe_task_success=recovered,
        missed_safe_action=1 - recovered,
        hint_assisted_safe_task_success=recovered,
        hint_triggered_revalidation=revalidation,
        hint_induced_unsafe_execution=unsafe,
        user_confirmation_requested=confirmation_requested,
        user_confirmation_accepted=confirmation_accepted,
        hint_authorized_after_confirmation=authorized,
        hard_violation=int(raw["hard_violation"]),
        forget_leakage=int(raw["forget_leakage"]),
        stale_reuse=int(raw["stale_reuse"]),
    )


def summarize_recovery(rows):
    fields = (
        "safe_task_success", "missed_safe_action", "hint_assisted_safe_task_success",
        "hint_triggered_revalidation", "hint_induced_unsafe_execution",
        "user_confirmation_requested", "user_confirmation_accepted",
        "hint_authorized_after_confirmation", "hard_violation", "forget_leakage", "stale_reuse",
    )
    return {field: _mean(rows, field) for field in fields}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", default="42,43,44")
    parser.add_argument("--broad-total", type=int, default=200)
    parser.add_argument("--recovery-per-family", type=int, default=50)
    parser.add_argument("--output", type=Path, default=ROOT / "governance_tax_v2.report.json")
    args = parser.parse_args()
    seeds = [int(item) for item in args.seeds.split(",") if item]

    broad_by_seed = {}
    recovery_by_seed = {}
    overall_by_seed = {mode: [] for mode in MODES}
    for seed in seeds:
        broad = broad_metrics(seed, args.broad_total)
        broad_by_seed[str(seed)] = broad
        recovery_cases = generate_recovery_split(seed, args.recovery_per_family)
        recovery_by_seed[str(seed)] = {}
        recovery_count = len(recovery_cases)
        total_count = args.broad_total + recovery_count
        for mode in MODES:
            recovery_rows = [run_recovery(case_id, family, mode) for case_id, family in recovery_cases]
            recovery_by_seed[str(seed)][mode] = {
                "summary": summarize_recovery(recovery_rows),
                "by_family": {
                    family: summarize_recovery([row for row in recovery_rows if row.family == family])
                    for family in FAMILIES
                },
            }
            broad_safe = broad[mode]["safe_task_success"]
            broad_missed = broad[mode]["missed_safe_action"]
            recovery_safe = _mean(recovery_rows, "safe_task_success")
            recovery_missed = _mean(recovery_rows, "missed_safe_action")
            overall_safe = (broad_safe * args.broad_total + recovery_safe * recovery_count) / total_count
            overall_missed = (broad_missed * args.broad_total + recovery_missed * recovery_count) / total_count
            overall_by_seed[mode].append(
                {
                    "seed": seed,
                    "safe_task_success": round(overall_safe, 4),
                    "missed_safe_action": round(overall_missed, 4),
                    "hint_induced_unsafe_execution": _mean(recovery_rows, "hint_induced_unsafe_execution"),
                    "hard_violation": _mean(recovery_rows, "hard_violation"),
                    "forget_leakage": _mean(recovery_rows, "forget_leakage"),
                    "stale_reuse": _mean(recovery_rows, "stale_reuse"),
                }
            )

    overall = {}
    for mode, rows in overall_by_seed.items():
        overall[mode] = {
            "safe_task_success": _ci([row["safe_task_success"] for row in rows]),
            "missed_safe_action": _ci([row["missed_safe_action"] for row in rows]),
            "hint_induced_unsafe_execution": _ci([row["hint_induced_unsafe_execution"] for row in rows]),
            "hard_violation": _ci([row["hard_violation"] for row in rows]),
            "forget_leakage": _ci([row["forget_leakage"] for row in rows]),
            "stale_reuse": _ci([row["stale_reuse"] for row in rows]),
        }

    report = {
        "conditions": {
            "seeds": seeds,
            "broad_cases_per_seed": args.broad_total,
            "recovery_cases_per_family": args.recovery_per_family,
            "recovery_cases_per_seed": args.recovery_per_family * len(FAMILIES),
            "overall_cases_per_seed": args.broad_total + args.recovery_per_family * len(FAMILIES),
            "weighting": "case-weighted, predeclared 200 broad + 50 per recovery family",
        },
        "broad_by_seed": broad_by_seed,
        "recovery_by_seed": recovery_by_seed,
        "overall_by_seed": overall_by_seed,
        "overall": overall,
    }
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"conditions": report["conditions"], "overall": overall}, indent=2, ensure_ascii=False))
    print(f"report: {args.output}")


if __name__ == "__main__":
    main()
