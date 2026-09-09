from dataclasses import replace

from eagle.db.orm import CandidateRecord, KnowledgeRecord, KnowledgeRevalidationRecord
from sqlalchemy import select
from test_governance import episode

from eagle.domain.scene import Scene
from eagle.governance import GovernanceService
from eagle.pack.service import PackService


class SearchGateway:
    def __init__(self, knowledge_id):
        self.knowledge_id = knowledge_id

    def search_knowledge(self, **_kwargs):
        return [
            {
                "id": "vector-1",
                "score": 0.9,
                "metadata": {"eagle_memory_id": self.knowledge_id},
            }
        ]


def test_conflicting_workflow_defers_then_commits_new_version(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(episode(session_id="s1", tool="libreoffice", fallback_from="wps"))
    governance.record_episode(episode(session_id="s2", tool="libreoffice", fallback_from="wps"))

    governance.record_episode(episode(session_id="s3", tool="onlyoffice", fallback_from="wps"))
    with session_factory() as session:
        old = session.scalar(select(KnowledgeRecord))
        deferred = session.scalar(select(CandidateRecord).where(CandidateRecord.candidate_key.contains("onlyoffice")))
    assert old.status == "ACTIVE"
    assert deferred.state == "DEFER"

    governance.record_episode(episode(session_id="s4", tool="onlyoffice", fallback_from="wps"))

    with session_factory() as session:
        knowledge = list(session.scalars(select(KnowledgeRecord).order_by(KnowledgeRecord.version)))
    assert [item.version for item in knowledge] == [1, 2]
    assert knowledge[1].parent_version_id == knowledge[0].id
    assert knowledge[1].lineage_id == knowledge[0].lineage_id
    assert knowledge[1].status == "ACTIVE"


def test_environment_revalidation_uses_execution_evidence_for_new_version(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(episode(session_id="s1", tool="libreoffice", fallback_from="wps"))
    governance.record_episode(episode(session_id="s2", tool="libreoffice", fallback_from="wps"))
    with session_factory() as session:
        old_id = session.scalar(select(KnowledgeRecord.id))
    with session_factory.begin() as session:
        session.get(KnowledgeRecord, old_id).mem0_id = "vector-1"

    PackService(session_factory, SearchGateway(old_id)).build(
        query="fallback",
        user_id="u1",
        scene=Scene(app="office", task="edit", artifact_type="docx"),
        environment_fingerprint="linux:wps-2",
    )
    original_environment_pack = PackService(session_factory, SearchGateway(old_id)).build(
        query="fallback",
        user_id="u1",
        scene=Scene(app="office", task="edit", artifact_type="docx"),
        environment_fingerprint="linux:wps-1",
    )
    assert [item["id"] for item in original_environment_pack.knowledge] == [old_id]
    new_environment_episode = replace(
        episode(tool="libreoffice", fallback_from="wps"),
        environment_fingerprint="linux:wps-2",
    )
    governance.record_episode(replace(new_environment_episode, session_id="s3", execution_id="env-2-execution-1"))
    governance.record_episode(replace(new_environment_episode, session_id="s4", execution_id="env-2-execution-2"))

    with session_factory() as session:
        knowledge = list(session.scalars(select(KnowledgeRecord).order_by(KnowledgeRecord.version)))
    assert [item.environment_fingerprint for item in knowledge] == [
        "linux:wps-1",
        "linux:wps-2",
    ]
    assert knowledge[1].parent_version_id == knowledge[0].id
    with session_factory() as session:
        request = session.scalar(select(KnowledgeRevalidationRecord))
    assert request.state == "DONE"
