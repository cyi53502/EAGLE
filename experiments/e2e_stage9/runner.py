"""Stage 9: Linux 真实端到端 4 场景闭环

Chain: Episode → Attribution → Candidate → Gate → SQLite COMMITTED
       → IndexJob → Worker → Mem0.add(infer=False)
       → KylinEmbedding(768) → KylinVectorStore(cosine_distance) → PACK

Uses deterministic shims (embedding_shim / vector_shim) — same Protocol as the
real kylin-ai-model-service + kylin-ai-vector-engine UDS stack, so the swap is
a single injection-site change.  Each scenario runs on an isolated :memory:
SQLite so results are reproducible on any Linux host.

Scenarios (EAGLE-experiment.md §9):
  1. Two identical fallbacks → Knowledge COMMITTED and PACK-retrievable.
  2. Explicit HARD preferred_tool → Planner action space filtered.
  3. Env drift → Revalidation Request, old env still serves knowledge.
  4. Forget → PACK immediately empty, after Worker vector+content erased.
"""

from __future__ import annotations

import os
import platform
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2] / "eagle_os_agent"
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select

from eagle.adapters.kylin.embedding_shim import ShimEmbeddingClient
from eagle.adapters.kylin.vector_shim import ShimVectorClient
from eagle.bootstrap import create_mem0_gateway
from eagle.db import create_schema, create_sqlite_engine, make_session_factory
from eagle.db.orm import IndexJobRecord, KnowledgeRecord, KnowledgeRevalidationRecord, PreferenceRecord
from eagle.domain.enums import IndexJobOperation, KnowledgeStatus
from eagle.domain.events import EpisodeInput, ExplicitPreferenceEvent
from eagle.domain.scene import Scene
from eagle.domain.enums import PreferenceHardness
from eagle.forgetting.service import ForgettingService
from eagle.governance import GovernanceService
from eagle.outbox.worker import IndexWorker
from eagle.pack.service import PackService


def _linux_fingerprint(suffix: str = "wps-1") -> str:
    try:
        os_id = Path("/etc/os-release").read_text().splitlines()
        codename = next((l.split("=", 1)[1].strip('"') for l in os_id if l.startswith("VERSION_CODENAME=")), "noble")
    except Exception:
        codename = "noble"
    return f"linux:{codename}:{suffix}"


def _gateway(tmpdir: str, collection: str):
    emb = ShimEmbeddingClient(dim=768)
    vec = ShimVectorClient()
    return create_mem0_gateway(
        embedding_client=emb,
        vector_client=vec,
        embedding_dims=768,
        distance_metric="cosine_distance",
        score_semantics="cosine_distance",
        history_db_path=os.path.join(tmpdir, "history.db"),
        collection_name=collection,
    )


def _drain_worker(session_factory, gateway) -> int:
    worker = IndexWorker(session_factory, gateway)
    n = 0
    while True:
        job_id = worker.process_next()
        if job_id is None:
            break
        n += 1
    return n


def scenario_1_fallback_commits_and_packs() -> dict:
    tmpdir = tempfile.mkdtemp(prefix="eagle-s9-s1-")
    gateway = _gateway(tmpdir, "eagle_e2e_s1")
    engine = create_sqlite_engine(":memory:")
    create_schema(engine)
    sf = make_session_factory(engine)
    gov = GovernanceService(sf)

    env = _linux_fingerprint("wps-1")
    ep1 = EpisodeInput(
        user_id="u1",
        session_id="s1",
        request_text="edit docx",
        scene=Scene(app="office", task="edit", artifact_type="docx"),
        tool_name="libreoffice",
        arguments_digest="d1",
        success=True,
        environment_fingerprint=env,
        fallback_from="wps",
        previous_error_code="E_OPEN",
        execution_id="s1-fb-1",
    )
    ep2 = EpisodeInput(
        user_id="u1",
        session_id="s2",
        request_text="edit docx",
        scene=Scene(app="office", task="edit", artifact_type="docx"),
        tool_name="libreoffice",
        arguments_digest="d2",
        success=True,
        environment_fingerprint=env,
        fallback_from="wps",
        previous_error_code="E_OPEN",
        execution_id="s2-fb-1",
    )
    r1 = gov.record_episode(ep1)
    r2 = gov.record_episode(ep2)

    with sf() as s:
        knowledge = list(s.scalars(select(KnowledgeRecord)))
        jobs = list(s.scalars(select(IndexJobRecord)))
    committed = [k for k in knowledge if k.status == KnowledgeStatus.ACTIVE.value]
    drained = _drain_worker(sf, gateway)
    with sf() as s:
        k_after = s.scalar(select(KnowledgeRecord))
        job_after = s.scalar(select(IndexJobRecord))

    pack = PackService(sf, gateway)
    ctx = pack.build(
        query="edit docx fallback",
        user_id="u1",
        scene=Scene(app="office", task="edit", artifact_type="docx"),
        environment_fingerprint=env,
    )
    passed = (
        len(committed) == 1
        and committed[0].environment_fingerprint == env
        and drained == 1
        and k_after is not None
        and k_after.mem0_id is not None
        and job_after is not None
        and job_after.state == "DONE"
        and len(ctx.knowledge) == 1
        and ctx.knowledge[0]["id"] == k_after.id
    )
    return {
        "name": "1-fallback-commits-and-packs",
        "passed": passed,
        "detail": {
            "r1_committed": r1.committed_memory_ids,
            "r2_committed": r2.committed_memory_ids,
            "knowledge_id": k_after.id if k_after else None,
            "mem0_id": k_after.mem0_id if k_after else None,
            "job_state": job_after.state if job_after else None,
            "pack_hits": len(ctx.knowledge),
            "env": env,
            "linux": platform.platform(),
        },
    }


def scenario_2_hard_preference_filters_tools() -> dict:
    tmpdir = tempfile.mkdtemp(prefix="eagle-s9-s2-")
    gateway = _gateway(tmpdir, "eagle_e2e_s2")
    engine = create_sqlite_engine(":memory:")
    create_schema(engine)
    sf = make_session_factory(engine)
    gov = GovernanceService(sf)

    env = _linux_fingerprint("wps-1")
    gov.record_episode(
        EpisodeInput(
            user_id="u1",
            session_id="s1",
            request_text="edit docx",
            scene=Scene(app="office", task="edit", artifact_type="docx"),
            tool_name="wps",
            arguments_digest="d1",
            success=True,
            environment_fingerprint=env,
            execution_id="s1-explicit-1",
        ),
        explicit_preferences=[
            ExplicitPreferenceEvent(
                key="preferred_tool",
                value={"tool": "wps"},
                hardness=PreferenceHardness.HARD,
                scene=Scene(artifact_type="docx"),
            )
        ],
    )

    with sf() as s:
        prefs = list(s.scalars(select(PreferenceRecord)))
    pack = PackService(sf, gateway)
    ctx = pack.build(
        query="edit",
        user_id="u1",
        scene=Scene(app="office", task="edit", artifact_type="docx"),
        environment_fingerprint=env,
    )
    from eagle.preference.compiler import apply_constraints

    class _Tool:
        def __init__(self, name, requires_network=False):
            self.name = name
            self.requires_network = requires_network

    tools = [_Tool("wps"), _Tool("libreoffice"), _Tool("onlyoffice")]
    allowed = [t.name for t in apply_constraints(tools, ctx.constraints)]

    passed = (
        len(prefs) == 1
        and prefs[0].hardness == "HARD"
        and ctx.constraints.allowed_tools == frozenset({"wps"})
        and allowed == ["wps"]
    )
    return {
        "name": "2-hard-preference-filters-tools",
        "passed": passed,
        "detail": {
            "allowed_tools": sorted(ctx.constraints.allowed_tools) if ctx.constraints.allowed_tools else None,
            "filtered": allowed,
            "env": env,
        },
    }


def scenario_3_env_drift_revalidation() -> dict:
    tmpdir = tempfile.mkdtemp(prefix="eagle-s9-s3-")
    gateway = _gateway(tmpdir, "eagle_e2e_s3")
    engine = create_sqlite_engine(":memory:")
    create_schema(engine)
    sf = make_session_factory(engine)
    gov = GovernanceService(sf)

    env_home = _linux_fingerprint("wps-1")
    env_drift = _linux_fingerprint("wps-2")

    for sid, eid in [("s1", "s3-fb-1"), ("s2", "s3-fb-2")]:
        gov.record_episode(
            EpisodeInput(
                user_id="u1",
                session_id=sid,
                request_text="edit docx",
                scene=Scene(app="office", task="edit", artifact_type="docx"),
                tool_name="libreoffice",
                arguments_digest=f"d-{sid}",
                success=True,
                environment_fingerprint=env_home,
                fallback_from="wps",
                previous_error_code="E_OPEN",
                execution_id=eid,
            )
        )
    _drain_worker(sf, gateway)
    with sf() as s:
        k = s.scalar(select(KnowledgeRecord))

    pack = PackService(sf, gateway)
    ctx_drift = pack.build(
        query="edit docx",
        user_id="u1",
        scene=Scene(app="office", task="edit", artifact_type="docx"),
        environment_fingerprint=env_drift,
    )
    with sf() as s:
        reqs = list(s.scalars(select(KnowledgeRevalidationRecord)))
        k_after = s.get(KnowledgeRecord, k.id)
    ctx_home = pack.build(
        query="edit docx",
        user_id="u1",
        scene=Scene(app="office", task="edit", artifact_type="docx"),
        environment_fingerprint=env_home,
    )
    passed = (
        len(ctx_drift.knowledge) == 0
        and len(reqs) == 1
        and reqs[0].target_environment == env_drift
        and k_after.status == KnowledgeStatus.ACTIVE.value
        and len(ctx_home.knowledge) == 1
        and ctx_home.knowledge[0]["id"] == k.id
    )
    return {
        "name": "3-env-drift-revalidation",
        "passed": passed,
        "detail": {
            "home_env": env_home,
            "drift_env": env_drift,
            "drift_hits": len(ctx_drift.knowledge),
            "home_hits": len(ctx_home.knowledge),
            "revalidation_target": reqs[0].target_environment if reqs else None,
            "knowledge_status": k_after.status if k_after else None,
        },
    }


def scenario_4_forget_immediate_and_worker_erases() -> dict:
    tmpdir = tempfile.mkdtemp(prefix="eagle-s9-s4-")
    gateway = _gateway(tmpdir, "eagle_e2e_s4")
    engine = create_sqlite_engine(":memory:")
    create_schema(engine)
    sf = make_session_factory(engine)
    gov = GovernanceService(sf)

    env = _linux_fingerprint("wps-1")
    for sid, eid in [("s1", "s4-fb-1"), ("s2", "s4-fb-2")]:
        gov.record_episode(
            EpisodeInput(
                user_id="u1",
                session_id=sid,
                request_text="edit docx",
                scene=Scene(app="office", task="edit", artifact_type="docx"),
                tool_name="libreoffice",
                arguments_digest=f"d-{sid}",
                success=True,
                environment_fingerprint=env,
                fallback_from="wps",
                previous_error_code="E_OPEN",
                execution_id=eid,
            )
        )
    _drain_worker(sf, gateway)
    with sf() as s:
        k = s.scalar(select(KnowledgeRecord))
        mem0_id_before = k.mem0_id
        kid = k.id

    pack = PackService(sf, gateway)
    ctx_before = pack.build(
        query="edit docx",
        user_id="u1",
        scene=Scene(app="office", task="edit", artifact_type="docx"),
        environment_fingerprint=env,
    )
    before_hits = len(ctx_before.knowledge)

    ForgettingService(sf).forget_knowledge(kid, user_id="u1")
    with sf() as s:
        k_forgetting = s.get(KnowledgeRecord, kid)
        delete_job = s.scalar(
            select(IndexJobRecord).where(
                IndexJobRecord.memory_id == kid,
                IndexJobRecord.operation == IndexJobOperation.DELETE.value,
            )
        )
    ctx_after_forget = pack.build(
        query="edit docx",
        user_id="u1",
        scene=Scene(app="office", task="edit", artifact_type="docx"),
        environment_fingerprint=env,
    )
    after_hits = len(ctx_after_forget.knowledge)
    status_forgetting = k_forgetting.status if k_forgetting else None
    job_state_before_worker = delete_job.state if delete_job else None

    drained = _drain_worker(sf, gateway)
    with sf() as s:
        k_final = s.get(KnowledgeRecord, kid)
        job_final = s.scalar(
            select(IndexJobRecord).where(
                IndexJobRecord.memory_id == kid,
                IndexJobRecord.operation == IndexJobOperation.DELETE.value,
            )
        )
        vec_client = gateway.memory.vector_store.client  # type: ignore[attr-defined]
        remaining = vec_client.get(collection="eagle_e2e_s4", vector_id=mem0_id_before, include_vector=False)

    ctx_after_worker = pack.build(
        query="edit docx",
        user_id="u1",
        scene=Scene(app="office", task="edit", artifact_type="docx"),
        environment_fingerprint=env,
    )

    passed = (
        before_hits == 1
        and after_hits == 0
        and status_forgetting == KnowledgeStatus.FORGETTING.value
        and job_state_before_worker == "PENDING"
        and drained == 1
        and k_final.status == KnowledgeStatus.FORGOTTEN.value
        and k_final.mem0_id is None
        and k_final.retrieval_text == ""
        and remaining is None
        and len(ctx_after_worker.knowledge) == 0
        and job_final.state == "DONE"
    )
    return {
        "name": "4-forget-and-worker-erases",
        "passed": passed,
        "detail": {
            "before_hits": before_hits,
            "after_forget_hits": after_hits,
            "after_worker_hits": len(ctx_after_worker.knowledge),
            "status_forgetting": status_forgetting,
            "status_final": k_final.status if k_final else None,
            "mem0_before": mem0_id_before,
            "vector_remaining": remaining,
            "job_before": job_state_before_worker,
            "job_after": job_final.state if job_final else None,
            "env": env,
        },
    }


def main() -> None:
    import json

    print(f"Linux: {platform.platform()}  env fingerprint base: {_linux_fingerprint('wps-1')}")
    scenarios = [
        scenario_1_fallback_commits_and_packs,
        scenario_2_hard_preference_filters_tools,
        scenario_3_env_drift_revalidation,
        scenario_4_forget_immediate_and_worker_erases,
    ]
    results = []
    for fn in scenarios:
        try:
            r = fn()
        except Exception as e:
            import traceback

            traceback.print_exc()
            r = {"name": fn.__name__, "passed": False, "detail": {"error": str(e)}}
        results.append(r)
        status = "PASS" if r["passed"] else "FAIL"
        print(f"[{status}] {r['name']}  {r['detail']}")

    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    print(f"\n{passed}/{total} scenarios passed")
    out = Path(__file__).parent / "report.json"
    out.write_text(json.dumps({"results": results, "passed": passed, "total": total}, indent=2, ensure_ascii=False))
    print(f"report: {out}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
