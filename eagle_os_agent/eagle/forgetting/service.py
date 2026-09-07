from sqlalchemy import delete, select

from eagle.db.orm import (
    CandidateRecord,
    EpisodeRecord,
    EvidenceRecord,
    IndexJobRecord,
    KnowledgeRecord,
    KnowledgeRevalidationRecord,
    MemoryEvidenceLinkRecord,
    PKVisibilityRecord,
    PreferenceRecord,
    utc_now,
)
from eagle.domain.enums import (
    IndexJobOperation,
    IndexJobState,
    KnowledgeStatus,
    PreferenceStatus,
)


class ForgettingService:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def forget_knowledge(self, knowledge_id: str, *, user_id: str) -> None:
        with self.session_factory.begin() as session:
            knowledge = session.scalar(
                select(KnowledgeRecord).where(
                    KnowledgeRecord.id == knowledge_id,
                    KnowledgeRecord.user_id == user_id,
                )
            )
            if knowledge is None:
                raise ValueError(f"Knowledge {knowledge_id} not found")
            if knowledge.status in {
                KnowledgeStatus.FORGETTING.value,
                KnowledgeStatus.FORGOTTEN.value,
            }:
                raise ValueError(f"Knowledge {knowledge_id} cannot be forgotten from status {knowledge.status}")

            knowledge.status = KnowledgeStatus.FORGETTING.value
            knowledge.updated_at = utc_now()
            session.execute(delete(PKVisibilityRecord).where(PKVisibilityRecord.knowledge_version_id == knowledge.id))
            session.add(
                IndexJobRecord(
                    index_key=f"DELETE:K:{knowledge.id}:{knowledge.version}",
                    memory_kind="K",
                    memory_id=knowledge.id,
                    operation=IndexJobOperation.DELETE.value,
                    state=IndexJobState.PENDING.value,
                    target_mem0_id=knowledge.mem0_id,
                )
            )

    def forget_preference(self, preference_id: str, *, user_id: str) -> None:
        with self.session_factory.begin() as session:
            preference = session.scalar(
                select(PreferenceRecord).where(
                    PreferenceRecord.id == preference_id,
                    PreferenceRecord.user_id == user_id,
                )
            )
            if preference is None:
                raise ValueError(f"Preference {preference_id} not found")
            if preference.status in {
                PreferenceStatus.FORGETTING.value,
                PreferenceStatus.FORGOTTEN.value,
            }:
                raise ValueError(f"Preference {preference_id} cannot be forgotten from status {preference.status}")

            session.execute(delete(PKVisibilityRecord).where(PKVisibilityRecord.preference_version_id == preference.id))
            self._erase_evidence(session, "P", preference.id)
            preference.preference_key = "__forgotten__"
            preference.preference_value_json = {}
            preference.scene_json = {}
            preference.confidence = 0.0
            preference.authorization_state = "FORGOTTEN"
            preference.mem0_id = None
            preference.status = PreferenceStatus.FORGOTTEN.value
            preference.updated_at = utc_now()

    @classmethod
    def finalize_knowledge(cls, session, knowledge: KnowledgeRecord) -> None:
        session.execute(delete(PKVisibilityRecord).where(PKVisibilityRecord.knowledge_version_id == knowledge.id))
        session.execute(
            delete(KnowledgeRevalidationRecord).where(KnowledgeRevalidationRecord.source_knowledge_id == knowledge.id)
        )
        cls._erase_evidence(session, "K", knowledge.id)
        knowledge.retrieval_text = ""
        knowledge.content_json = {}
        knowledge.scene_json = {}
        knowledge.environment_fingerprint = ""
        knowledge.confidence = 0.0
        knowledge.mem0_id = None
        knowledge.status = KnowledgeStatus.FORGOTTEN.value
        knowledge.updated_at = utc_now()

    @staticmethod
    def _erase_evidence(session, memory_kind: str, memory_id: str) -> None:
        links = list(
            session.scalars(
                select(MemoryEvidenceLinkRecord).where(
                    MemoryEvidenceLinkRecord.memory_kind == memory_kind,
                    MemoryEvidenceLinkRecord.memory_id == memory_id,
                )
            )
        )
        evidence_ids = [link.evidence_id for link in links]
        for link in links:
            session.delete(link)
        session.flush()

        candidate_ids = set()
        episode_ids = set()
        for evidence_id in evidence_ids:
            still_linked = session.scalar(
                select(MemoryEvidenceLinkRecord.id).where(MemoryEvidenceLinkRecord.evidence_id == evidence_id)
            )
            if still_linked is not None:
                continue
            evidence = session.get(EvidenceRecord, evidence_id)
            candidate_ids.add(evidence.candidate_id)
            episode_ids.add(evidence.episode_id)
            session.delete(evidence)
        session.flush()

        for candidate_id in candidate_ids:
            if session.scalar(select(EvidenceRecord.id).where(EvidenceRecord.candidate_id == candidate_id)) is None:
                candidate = session.get(CandidateRecord, candidate_id)
                session.delete(candidate)
        for episode_id in episode_ids:
            episode = session.get(EpisodeRecord, episode_id)
            remaining_candidate_ids = set(
                session.scalars(select(EvidenceRecord.candidate_id).where(EvidenceRecord.episode_id == episode_id))
            )
            if not remaining_candidate_ids:
                episode.scene_json = {}
                episode.request_text = ""
                episode.tool_name = "__forgotten__"
                episode.arguments_digest = ""
                episode.result_class = None
                episode.error_code = None
                episode.fallback_from = None
                episode.previous_error_code = None
                episode.user_correction_json = None
                episode.environment_fingerprint = ""
                episode.ingest_fingerprint = "FORGOTTEN"
                episode.governance_result_json = {
                    "episode_id": episode.id,
                    "candidate_ids": [],
                    "committed_memory_ids": [],
                }
            else:
                stored = dict(episode.governance_result_json)
                stored["candidate_ids"] = [
                    candidate_id for candidate_id in stored["candidate_ids"] if candidate_id in remaining_candidate_ids
                ]
                stored["committed_memory_ids"] = [
                    stored_memory_id
                    for stored_memory_id in stored["committed_memory_ids"]
                    if stored_memory_id != memory_id
                ]
                episode.governance_result_json = stored
