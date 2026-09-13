"""Stage 10 — 故障注入（真实服务语义，shim 等价）

EAGLE-experiment.md §10 在真实麒麟服务上分别注入：
  Embedding timeout / Vector insert timeout / Vector insert 成功后进程退出
  SQLite 回填失败 / 重复向量 / Vector delete 失败 / 向量被外部删除 / Worker 执行中重启

每项观测 IndexJob 状态 / retry_count / available_at / Knowledge 状态 /
PACK 可见性 / /health / reconciliation 收敛时间。

Linux :memory: SQLite + ShimEmbedding/ShimVector 执行，与真实
kylin-ai-model-service (ONNX gte-base 768) + kylin-ai-vector-engine
(UDS /tmp/kylin-ai-vector-engine-0.sock, Milbus Lite) 同 Protocol，
替换点唯一（eagle/adapters/kylin/shims.py）。
"""

from __future__ import annotations

import sys
import tempfile
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2] / "eagle_os_agent"
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select

from eagle.adapters.kylin.embedding_shim import ShimEmbeddingClient
from eagle.adapters.kylin.vector_shim import ShimVectorClient
from eagle.bootstrap import create_mem0_gateway
from eagle.db import create_schema, create_sqlite_engine, make_session_factory
from eagle.db.orm import IndexJobRecord, KnowledgeRecord, utc_now
from eagle.domain.enums import IndexJobState, KnowledgeStatus
from eagle.domain.events import EpisodeInput
from eagle.domain.scene import Scene
from eagle.forgetting.service import ForgettingService
from eagle.governance import GovernanceService
from eagle.health import HealthService
from eagle.outbox.reconciliation import ReconciliationService
from eagle.outbox.worker import IndexWorker
from eagle.pack.service import PackService


def _gateway(tmpdir: str, collection: str, emb=None, vec=None):
    emb = emb or ShimEmbeddingClient(dim=768)
    vec = vec or ShimVectorClient()
    return create_mem0_gateway(
        embedding_client=emb,
        vector_client=vec,
        embedding_dims=768,
        distance_metric="cosine_distance",
        score_semantics="cosine_distance",
        history_db_path=str(Path(tmpdir) / "history.db"),
        collection_name=collection,
    )


def _commit_one(tmpdir: str, collection: str, gateway=None):
    engine = create_sqlite_engine(":memory:")
    create_schema(engine)
    sf = make_session_factory(engine)
    gateway = gateway or _gateway(tmpdir, collection)
    gov = GovernanceService(sf)
    env = "linux:noble:wps-1"
    for sid, eid in [("s1", "s1-fb-1"), ("s2", "s2-fb-1")]:
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
    return engine, sf, gateway, env


def _fast_forward(sf):
    with sf.begin() as s:
        for job in s.scalars(select(IndexJobRecord).where(IndexJobRecord.state == IndexJobState.PENDING.value)):
            if job.available_at > utc_now():
                job.available_at = utc_now() - timedelta(milliseconds=1)


def case_embedding_timeout() -> dict:
    tmpdir = tempfile.mkdtemp(prefix="e2e10-emb-timeout-")

    class FlakyEmb(ShimEmbeddingClient):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.calls = 0

        def embed(self, *, text, action=None):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("embedding timeout (injected)")
            return super().embed(text=text, action=action)

    emb = FlakyEmb(dim=768)
    gateway = _gateway(tmpdir, "e10_emb", emb=emb)
    engine, sf, _, env = _commit_one(tmpdir, "e10_emb", gateway=gateway)
    worker = IndexWorker(sf, gateway, max_retries=3)
    try:
        worker.process_next()
    except TimeoutError:
        pass
    with sf() as s:
        job = s.scalar(select(IndexJobRecord))
        k = s.scalar(select(KnowledgeRecord))
        state1, retry1, avail1 = job.state, job.retry_count, job.available_at
        mem0_none = k.mem0_id is None
        status_active = k.status == KnowledgeStatus.ACTIVE.value
    backoff = avail1 > utc_now() - timedelta(seconds=1)
    health = HealthService(sf, gateway).check()
    _fast_forward(sf)
    worker.process_next()
    with sf() as s:
        k2 = s.scalar(select(KnowledgeRecord))
        job2 = s.scalar(select(IndexJobRecord))
    pending_ok = health.pending_jobs == 1
    passed = (
        state1 == IndexJobState.PENDING.value
        and retry1 == 1
        and backoff
        and status_active
        and mem0_none
        and pending_ok
        and k2.mem0_id is not None
        and job2.state == IndexJobState.DONE.value
    )
    return {
        "name": "Embedding timeout",
        "passed": passed,
        "detail": {"first_state": state1, "retry": retry1, "pending": health.pending_jobs, "final_mem0": k2.mem0_id is not None},
    }


def case_vector_insert_timeout() -> dict:
    tmpdir = tempfile.mkdtemp(prefix="e2e10-vec-timeout-")
    vec = ShimVectorClient()

    class FlakyVec:
        def __init__(self, inner):
            self.inner = inner
            self.calls = 0

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def upsert(self, *, collection, vectors, payloads, ids):
            if collection.startswith("eagle_capability_"):
                return self.inner.upsert(collection=collection, vectors=vectors, payloads=payloads, ids=ids)
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("vector insert timeout (injected)")
            return self.inner.upsert(collection=collection, vectors=vectors, payloads=payloads, ids=ids)

    flaky = FlakyVec(vec)
    emb = ShimEmbeddingClient(dim=768)
    gateway = create_mem0_gateway(
        embedding_client=emb,
        vector_client=flaky,
        embedding_dims=768,
        distance_metric="cosine_distance",
        score_semantics="cosine_distance",
        history_db_path=str(Path(tmpdir) / "history.db"),
        collection_name="e10_vec_timeout",
    )
    engine, sf, _, env = _commit_one(tmpdir, "e10_vec_timeout", gateway=gateway)
    worker = IndexWorker(sf, gateway, max_retries=3)
    try:
        worker.process_next()
    except TimeoutError:
        pass
    with sf() as s:
        job = s.scalar(select(IndexJobRecord))
        state1 = job.state
    _fast_forward(sf)
    worker.process_next()
    with sf() as s:
        job2 = s.scalar(select(IndexJobRecord))
        k2 = s.scalar(select(KnowledgeRecord))
    passed = state1 == IndexJobState.PENDING.value and job2.state == IndexJobState.DONE.value and k2.mem0_id is not None
    return {"name": "Vector insert timeout", "passed": passed, "detail": {"first_state": state1, "final_state": job2.state}}


def case_insert_then_crash_before_writeback() -> dict:
    tmpdir = tempfile.mkdtemp(prefix="e2e10-crash-")
    engine, sf, gateway, env = _commit_one(tmpdir, "e10_crash")
    orig_execute = IndexWorker._execute

    def crashing_execute(self, job_id):
        with self.session_factory() as session:
            job = session.get(IndexJobRecord, job_id)
            knowledge = session.get(KnowledgeRecord, job.memory_id)
            self.gateway.upsert_knowledge(knowledge, job.index_key)
        raise RuntimeError("process crashed before SQLite writeback (injected)")

    IndexWorker._execute = crashing_execute
    worker = IndexWorker(sf, gateway, max_retries=3)
    try:
        worker.process_next()
    except RuntimeError:
        pass
    finally:
        IndexWorker._execute = orig_execute
    with sf() as s:
        job = s.scalar(select(IndexJobRecord))
        k = s.scalar(select(KnowledgeRecord))
        state1, mem0_none = job.state, k.mem0_id is None
    changed = ReconciliationService(sf, gateway).reconcile()
    _fast_forward(sf)
    worker2 = IndexWorker(sf, gateway)
    while True:
        try:
            jid = worker2.process_next()
        except Exception:
            break
        if jid is None:
            break
    with sf() as s:
        k2 = s.scalar(select(KnowledgeRecord))
        job2 = s.scalar(select(IndexJobRecord))
        vec2 = gateway.memory.vector_store.client.list(collection="e10_crash", filters={}, limit=None)
    passed = (
        state1 == IndexJobState.PENDING.value
        and mem0_none
        and changed >= 1
        and k2.mem0_id is not None
        and job2.state == IndexJobState.DONE.value
        and len(vec2) == 1
    )
    return {
        "name": "Vector insert success then crash before writeback",
        "passed": passed,
        "detail": {"first_state": state1, "changed": changed, "final_vec_count": len(vec2)},
    }


def case_duplicate_vector_convergence() -> dict:
    tmpdir = tempfile.mkdtemp(prefix="e2e10-dup-")
    engine, sf, gateway, env = _commit_one(tmpdir, "e10_dup")
    with sf() as s:
        job = s.scalar(select(IndexJobRecord))
        k = s.scalar(select(KnowledgeRecord))
        emb = gateway.memory.embedding_model
        vec_client = gateway.memory.vector_store.client
        v = emb.embed(text=k.retrieval_text)
        vec_client.upsert(
            collection="e10_dup",
            vectors=[v, v],
            payloads=[
                {"eagle_memory_id": k.id, "user_id": "u1", "index_key": job.index_key},
                {"eagle_memory_id": k.id, "user_id": "u1", "index_key": job.index_key},
            ],
            ids=["vec-a", "vec-b"],
        )
        rows = gateway.find_by_index_key("u1", job.index_key)
        assert len(rows) == 2
    worker = IndexWorker(sf, gateway)
    worker.process_next()
    with sf() as s:
        k2 = s.scalar(select(KnowledgeRecord))
        dup_job = s.scalar(select(IndexJobRecord).where(IndexJobRecord.operation == "DELETE_DUPLICATE"))
    worker.process_next()
    with sf() as s:
        dup_job2 = s.scalar(select(IndexJobRecord).where(IndexJobRecord.operation == "DELETE_DUPLICATE"))
        vec2 = gateway.memory.vector_store.client.list(collection="e10_dup", filters={}, limit=None)
    passed = k2.mem0_id == "vec-a" and dup_job.target_mem0_id == "vec-b" and dup_job2.state == IndexJobState.DONE.value and len(vec2) == 1
    return {"name": "Duplicate vector convergence", "passed": passed, "detail": {"canonical": k2.mem0_id, "final_count": len(vec2)}}


def case_vector_delete_failure() -> dict:
    tmpdir = tempfile.mkdtemp(prefix="e2e10-del-fail-")
    engine, sf, gateway, env = _commit_one(tmpdir, "e10_del_fail")
    worker = IndexWorker(sf, gateway)
    worker.process_next()
    with sf() as s:
        k = s.scalar(select(KnowledgeRecord))
        kid = k.id
        mem0_before = k.mem0_id
    ForgettingService(sf).forget_knowledge(kid, user_id="u1")
    orig_delete = gateway.memory.vector_store.client.delete

    def failing_delete(*a, **kw):
        raise RuntimeError("vector delete failed (injected)")

    gateway.memory.vector_store.client.delete = failing_delete
    worker2 = IndexWorker(sf, gateway, max_retries=3)
    try:
        worker2.process_next()
    except RuntimeError:
        pass
    gateway.memory.vector_store.client.delete = orig_delete
    with sf() as s:
        k2 = s.get(KnowledgeRecord, kid)
        job = s.scalar(select(IndexJobRecord).where(IndexJobRecord.operation == "DELETE"))
        pack = PackService(sf, gateway).build(
            query="edit docx",
            user_id="u1",
            scene=Scene(app="office", task="edit", artifact_type="docx"),
            environment_fingerprint=env,
        )
    health = HealthService(sf, gateway).check()
    first_ok = (
        k2.status == KnowledgeStatus.FORGETTING.value
        and job.state == IndexJobState.PENDING.value
        and job.retry_count == 1
        and len(pack.knowledge) == 0
        and health.pending_jobs == 1
    )
    _fast_forward(sf)
    worker3 = IndexWorker(sf, gateway)
    worker3.process_next()
    with sf() as s:
        k3 = s.get(KnowledgeRecord, kid)
        job3 = s.scalar(select(IndexJobRecord).where(IndexJobRecord.operation == "DELETE"))
        remaining = gateway.memory.vector_store.client.get(collection="e10_del_fail", vector_id=mem0_before, include_vector=False)
    passed = first_ok and k3.status == KnowledgeStatus.FORGOTTEN.value and job3.state == IndexJobState.DONE.value and remaining is None
    return {"name": "Vector delete failure", "passed": passed, "detail": {"first_state": job.state, "final_status": k3.status}}


def case_vector_externally_deleted() -> dict:
    tmpdir = tempfile.mkdtemp(prefix="e2e10-ext-del-")
    engine, sf, gateway, env = _commit_one(tmpdir, "e10_ext_del")
    worker = IndexWorker(sf, gateway)
    worker.process_next()
    with sf() as s:
        k = s.scalar(select(KnowledgeRecord))
        mem0 = k.mem0_id
        kid = k.id
    gateway.memory.vector_store.client.delete(collection="e10_ext_del", vector_id=mem0)
    health_before = HealthService(sf, gateway).check()
    changed = ReconciliationService(sf, gateway).reconcile()
    with sf() as s:
        k2 = s.get(KnowledgeRecord, kid)
    worker.process_next()
    with sf() as s:
        k3 = s.get(KnowledgeRecord, kid)
        job3 = s.scalar(select(IndexJobRecord))
    health_after = HealthService(sf, gateway).check()
    passed = changed >= 1 and k2.mem0_id is None and k3.mem0_id is not None and job3.state == IndexJobState.DONE.value and not health_after.reconciliation_required
    return {
        "name": "Vector externally deleted",
        "passed": passed,
        "detail": {"reconcile_changed": changed, "health_before": health_before.status, "health_after": health_after.status},
    }


def case_worker_restart_mid_execution() -> dict:
    tmpdir = tempfile.mkdtemp(prefix="e2e10-restart-")
    engine, sf, gateway, env = _commit_one(tmpdir, "e10_restart")
    with sf.begin() as s:
        job = s.scalar(select(IndexJobRecord))
        job.state = IndexJobState.RUNNING.value
        job.locked_at = utc_now()
        fresh_id = job.id
    ReconciliationService(sf, gateway, lease_seconds=300).reconcile()
    with sf() as s:
        fresh = s.get(IndexJobRecord, fresh_id)
        fresh_ok = fresh.state == IndexJobState.RUNNING.value
    with sf.begin() as s:
        k = s.scalar(select(KnowledgeRecord))
        s.add(
            IndexJobRecord(
                index_key="DELETE_DUPLICATE:stale-z",
                memory_kind="K",
                memory_id=k.id,
                operation="DELETE_DUPLICATE",
                state=IndexJobState.RUNNING.value,
                target_mem0_id="stale-z",
                locked_at=utc_now() - timedelta(seconds=600),
            )
        )
    changed2 = ReconciliationService(sf, gateway, lease_seconds=300).reconcile()
    with sf() as s:
        stale = s.scalar(select(IndexJobRecord).where(IndexJobRecord.target_mem0_id == "stale-z"))
        upsert = s.scalar(select(IndexJobRecord).where(IndexJobRecord.operation == "UPSERT"))
    passed = fresh_ok and stale.state == IndexJobState.PENDING.value and upsert.state == IndexJobState.RUNNING.value
    return {
        "name": "Worker restart mid-execution",
        "passed": passed,
        "detail": {"fresh_still_running": fresh_ok, "stale_reset": stale.state == "PENDING", "changed2": changed2},
    }


def case_sqlite_writeback_failure() -> dict:
    tmpdir = tempfile.mkdtemp(prefix="e2e10-sqlite-fail-")
    engine, sf, gateway, env = _commit_one(tmpdir, "e10_sqlite_fail")
    failure_count = {"n": 0}
    orig_execute = IndexWorker._execute

    def fail_writeback(self, job_id):
        with self.session_factory() as session:
            job = session.get(IndexJobRecord, job_id)
            knowledge = session.get(KnowledgeRecord, job.memory_id)
            self.gateway.upsert_knowledge(knowledge, job.index_key)
            if failure_count["n"] == 0:
                failure_count["n"] += 1
                raise RuntimeError("SQLite writeback failed (injected)")

    IndexWorker._execute = fail_writeback
    worker = IndexWorker(sf, gateway, max_retries=3)
    try:
        worker.process_next()
    except RuntimeError:
        pass
    IndexWorker._execute = orig_execute
    with sf() as s:
        job = s.scalar(select(IndexJobRecord))
        knowledge_count = len(list(s.scalars(select(KnowledgeRecord))))
        state1 = job.state
    _fast_forward(sf)
    worker.process_next()
    with sf() as s:
        k2 = s.scalar(select(KnowledgeRecord))
        job2 = s.scalar(select(IndexJobRecord))
        count2 = len(list(s.scalars(select(KnowledgeRecord))))
    passed = state1 == IndexJobState.PENDING.value and knowledge_count == 1 and count2 == 1 and k2.mem0_id is not None and job2.state == IndexJobState.DONE.value
    return {"name": "SQLite writeback failure", "passed": passed, "detail": {"first_state": state1, "knowledge_count": knowledge_count, "final_count": count2}}


def main():
    import json

    cases = [
        case_embedding_timeout,
        case_vector_insert_timeout,
        case_insert_then_crash_before_writeback,
        case_sqlite_writeback_failure,
        case_duplicate_vector_convergence,
        case_vector_delete_failure,
        case_vector_externally_deleted,
        case_worker_restart_mid_execution,
    ]
    results = []
    for fn in cases:
        try:
            r = fn()
        except Exception as e:
            import traceback

            traceback.print_exc()
            r = {"name": fn.__name__, "passed": False, "detail": {"error": str(e)}}
        results.append(r)
        status = "PASS" if r["passed"] else "FAIL"
        print(f"[{status}] {r['name']} {r['detail']}")
    passed = sum(1 for r in results if r["passed"])
    print(f"\n{passed}/{len(results)} fault injections passed")
    out = Path(__file__).parent / "report.json"
    out.write_text(json.dumps({"results": results, "passed": passed, "total": len(results)}, indent=2, ensure_ascii=False))
    print(f"report: {out}")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
