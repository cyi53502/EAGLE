from datetime import timedelta

from eagle.db.orm import IndexJobRecord, KnowledgeRecord, utc_now
from sqlalchemy import select
from test_governance import episode

from eagle.governance import GovernanceService
from eagle.outbox.reconciliation import ReconciliationService


class Gateway:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def find_by_index_key(self, user_id, index_key):
        return self.rows


def test_reconciliation_requeues_active_knowledge_missing_index(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(episode(session_id="s1", tool="libreoffice", fallback_from="wps"))
    governance.record_episode(episode(session_id="s2", tool="libreoffice", fallback_from="wps"))
    with session_factory.begin() as session:
        job = session.scalar(select(IndexJobRecord))
        job.state = "DONE"

    changed = ReconciliationService(session_factory, Gateway()).reconcile()

    assert changed == 1
    with session_factory() as session:
        job = session.scalar(select(IndexJobRecord))
    assert job.state == "PENDING"


def test_reconciliation_detects_missing_vector_despite_stored_mem0_id(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(episode(session_id="s1", tool="libreoffice", fallback_from="wps"))
    governance.record_episode(episode(session_id="s2", tool="libreoffice", fallback_from="wps"))
    with session_factory.begin() as session:
        job = session.scalar(select(IndexJobRecord))
        job.state = "DONE"
        knowledge = session.get(KnowledgeRecord, job.memory_id)
        knowledge.mem0_id = "missing-vector"

    changed = ReconciliationService(session_factory, Gateway()).reconcile()

    assert changed == 2
    with session_factory() as session:
        job = session.scalar(select(IndexJobRecord))
        knowledge = session.get(KnowledgeRecord, job.memory_id)
    assert job.state == "PENDING"
    assert knowledge.mem0_id is None


def test_reconciliation_selects_canonical_and_schedules_duplicate_cleanup(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(episode(session_id="s1", tool="libreoffice", fallback_from="wps"))
    governance.record_episode(episode(session_id="s2", tool="libreoffice", fallback_from="wps"))

    changed = ReconciliationService(
        session_factory,
        Gateway(rows=[{"id": "vector-b"}, {"id": "vector-a"}]),
    ).reconcile()

    assert changed == 3
    with session_factory() as session:
        knowledge = session.scalar(select(KnowledgeRecord))
        upsert_job = session.scalar(select(IndexJobRecord).where(IndexJobRecord.operation == "UPSERT"))
        duplicate_job = session.scalar(select(IndexJobRecord).where(IndexJobRecord.operation == "DELETE_DUPLICATE"))
    assert knowledge.mem0_id == "vector-a"
    assert upsert_job.state == "DONE"
    assert duplicate_job.target_mem0_id == "vector-b"


def test_reconciliation_resets_running_jobs_past_lease(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(episode(session_id="s1", tool="libreoffice", fallback_from="wps"))
    governance.record_episode(episode(session_id="s2", tool="libreoffice", fallback_from="wps"))
    with session_factory.begin() as session:
        knowledge_id = session.scalar(select(KnowledgeRecord.id))
        session.add(
            IndexJobRecord(
                index_key="DELETE_DUPLICATE:vector-z",
                memory_kind="K",
                memory_id=knowledge_id,
                operation="DELETE_DUPLICATE",
                state="RUNNING",
                target_mem0_id="vector-z",
                locked_at=utc_now() - timedelta(seconds=600),
            )
        )
        session.add(
            IndexJobRecord(
                index_key="DELETE_DUPLICATE:vector-y",
                memory_kind="K",
                memory_id=knowledge_id,
                operation="DELETE_DUPLICATE",
                state="RUNNING",
                target_mem0_id="vector-y",
                locked_at=utc_now(),
            )
        )

    changed = ReconciliationService(
        session_factory,
        Gateway(rows=[{"id": "vector-a"}]),
        lease_seconds=300,
    ).reconcile()

    with session_factory() as session:
        stale_job = session.scalar(select(IndexJobRecord).where(IndexJobRecord.target_mem0_id == "vector-z"))
        fresh_job = session.scalar(select(IndexJobRecord).where(IndexJobRecord.target_mem0_id == "vector-y"))
        upsert_job = session.scalar(select(IndexJobRecord).where(IndexJobRecord.operation == "UPSERT"))
        knowledge = session.get(KnowledgeRecord, knowledge_id)
    assert stale_job.state == "PENDING"
    assert stale_job.locked_at is None
    assert fresh_job.state == "RUNNING"
    assert fresh_job.locked_at is not None
    assert upsert_job.state == "DONE"
    assert knowledge.mem0_id == "vector-a"
    assert changed == 3


def test_reconciliation_does_not_requeue_fresh_running_upsert(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(episode(session_id="s1", tool="libreoffice", fallback_from="wps"))
    governance.record_episode(episode(session_id="s2", tool="libreoffice", fallback_from="wps"))
    with session_factory.begin() as session:
        job = session.scalar(select(IndexJobRecord))
        job.state = "RUNNING"
        job.locked_at = utc_now()

    changed = ReconciliationService(session_factory, Gateway(), lease_seconds=300).reconcile()

    with session_factory() as session:
        job = session.scalar(select(IndexJobRecord))
    assert changed == 0
    assert job.state == "RUNNING"
    assert job.locked_at is not None
