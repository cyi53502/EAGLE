from sqlalchemy import select
from test_governance import episode

from eagle.db.orm import IndexJobRecord, KnowledgeRecord
from eagle.governance import GovernanceService
from eagle.health import HealthService


class Gateway:
    def __init__(self, embedding_ready=True, vector_ready=True):
        self._embedding_ready = embedding_ready
        self._vector_ready = vector_ready

    def embedding_ready(self):
        return self._embedding_ready

    def vector_ready(self):
        return self._vector_ready

    def find_by_index_key(self, user_id, index_key):
        return []


def test_health_exposes_required_runtime_fields(session_factory):
    report = HealthService(session_factory, Gateway()).check().to_dict()

    assert report == {
        "status": "ok",
        "database_ready": True,
        "mem0_ready": True,
        "kylin_embedding_ready": True,
        "kylin_vector_ready": True,
        "pending_jobs": 0,
        "failed_jobs": 0,
        "reconciliation_required": False,
        "errors": (),
    }


def test_health_exposes_failed_job_and_reconciliation_requirement(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(episode(session_id="s1", tool="libreoffice", fallback_from="wps"))
    governance.record_episode(episode(session_id="s2", tool="libreoffice", fallback_from="wps"))
    with session_factory.begin() as session:
        job = session.scalar(select(IndexJobRecord))
        job.state = "FAILED"

    report = HealthService(session_factory, Gateway()).check()

    assert report.status == "degraded"
    assert report.failed_jobs == 1
    assert report.reconciliation_required is True


def test_health_reports_kylin_readiness_failure(session_factory):
    report = HealthService(session_factory, Gateway(vector_ready=False)).check()

    assert report.status == "degraded"
    assert report.mem0_ready is False
    assert report.kylin_embedding_ready is True
    assert report.kylin_vector_ready is False


def test_health_detects_vector_missing_behind_stored_mem0_id(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(episode(session_id="s1", tool="libreoffice", fallback_from="wps"))
    governance.record_episode(episode(session_id="s2", tool="libreoffice", fallback_from="wps"))
    with session_factory.begin() as session:
        knowledge = session.scalar(select(KnowledgeRecord))
        knowledge.mem0_id = "missing-vector"
        session.scalar(select(IndexJobRecord)).state = "DONE"

    report = HealthService(session_factory, Gateway()).check()

    assert report.status == "degraded"
    assert report.reconciliation_required is True
