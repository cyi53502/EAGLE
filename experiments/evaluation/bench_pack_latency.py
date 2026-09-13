#!/usr/bin/env python3
"""PACK build latency p50/p95/mean + embedding single-shot latency.

Honors KYLIN_USE_SHIM (default 1 → deterministic shims; "0" → Real ONNX
embedding when KYLIN_EMBEDDING_MODEL points at weights + real vector engine
over KYLIN_VECTOR_UDS).  The reported ``backend`` is the honest client tag, so
a shim fallback run is never mislabeled as real.

Mirrors stage 11 conditions: same EAGLE-Gov cases (seed 42, 200), top_k=5,
query="fallback", full governance record + IndexWorker drain before probing.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2] / "eagle_os_agent"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stage11_12"))

import runner  # noqa: E402

from eagle.bootstrap import create_auto_gateway  # noqa: E402
from eagle.db import create_schema, create_sqlite_engine, make_session_factory  # noqa: E402
from eagle.domain.enums import PreferenceHardness  # noqa: E402
from eagle.domain.events import EpisodeInput, ExplicitPreferenceEvent, UserCorrectionEvent  # noqa: E402
from eagle.domain.scene import Scene  # noqa: E402
from eagle.governance import GovernanceService  # noqa: E402
from eagle.pack.service import PackService  # noqa: E402


def _embedding_client(gateway):
    model = getattr(gateway.memory, "embedding_model", None)
    return getattr(model, "client", model)


def _percentile(sorted_ms: list[float], p: float) -> float:
    idx = min(len(sorted_ms) - 1, max(0, int(len(sorted_ms) * p)))
    return round(sorted_ms[idx], 2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--total", type=int, default=200)
    parser.add_argument(
        "--out", type=Path, default=Path(__file__).with_name("bench_pack_latency.json")
    )
    args = parser.parse_args()

    cases = [case.to_json() for case in runner._generate.generate(args.seed, args.total)]
    collection = f"eagle_pack_latency_{args.seed}"

    # One shared gateway (ONNX session + engine connection reused).  Searches
    # are scoped by each case's eligible-memory id allowlist, so accumulating
    # rows in the shared collection never leaks across cases.  The governance
    # SQLite is per-case (mirrors stage 11 run_case_arm_c; execution_ids and
    # replay dedup are case-local, so a shared store would false-positive).
    gw = create_auto_gateway(
        history_db_path=runner.tempfile_path(collection), collection_name=collection
    )

    # --- index + probe each case through the full governance chain (arm C) ---
    latencies: list[float] = []
    for case in cases:
        engine = create_sqlite_engine(":memory:")
        create_schema(engine)
        session_factory = make_session_factory(engine)
        gov = GovernanceService(session_factory)
        pack = PackService(session_factory, gw)

        for position, raw in enumerate(case["episodes"], start=1):
            correction = raw.get("user_correction")
            ep = EpisodeInput(
                user_id=case["user_id"],
                session_id=raw["session_id"],
                request_text=raw["request_text"],
                scene=Scene(**raw["scene"]),
                tool_name=raw["tool_name"],
                arguments_digest=raw["arguments_digest"],
                success=raw["success"],
                environment_fingerprint=raw["environment_fingerprint"],
                execution_id=raw.get("execution_id", f"{case['id']}-exec-{position}"),
                fallback_from=raw.get("fallback_from"),
                previous_error_code=raw.get("previous_error_code"),
                user_intervention=raw.get("user_intervention", False),
                user_correction=(
                    UserCorrectionEvent(
                        candidate_type=correction["candidate_type"],
                        key=correction["key"],
                        value=correction["value"],
                        scene=Scene(**correction.get("scene", {})),
                    )
                    if correction
                    else None
                ),
            )
            gov.record_episode(
                ep,
                explicit_preferences=[
                    ExplicitPreferenceEvent(
                        key=spec["key"],
                        value=spec["value"],
                        hardness=PreferenceHardness(spec["hardness"]),
                        scene=Scene(**spec.get("scene", {})),
                    )
                    for spec in raw.get("explicit_preferences", [])
                ],
            )
        runner._drain(session_factory, gw)

        oracle = runner.build_oracle(case)
        scene = Scene(**oracle.probe_scene)
        t0 = time.perf_counter()
        try:
            pack.build(
                query="fallback",
                user_id=case["user_id"],
                scene=scene,
                environment_fingerprint=oracle.env_home,
                top_k=runner.TOP_K,
            )
        except ValueError:
            pass  # unenforceable HARD key fails fast (correct-by-design, as in stage 11)
        latencies.append((time.perf_counter() - t0) * 1000.0)
    latencies.sort()
    data = {
        "seed": args.seed,
        "total": len(cases),
        "backend": gw.backend,
        "pack_latency_ms": {
            "n": len(latencies),
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
            "mean": round(statistics.mean(latencies), 2),
            "max": round(latencies[-1], 2),
        },
    }

    # --- embedding single-shot latency ---
    emb = _embedding_client(gw)
    t0 = time.perf_counter()
    emb.embed(text="edit docx")
    data["embed_single_ms"] = round((time.perf_counter() - t0) * 1000.0, 3)

    data["pass"] = data["pack_latency_ms"]["p95"] < 500.0
    data["out"] = str(args.out)
    args.out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    main()
