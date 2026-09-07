import json

from sqlalchemy import select

from eagle.db.orm import (
    CandidateRecord,
    EvidenceRecord,
    IndexJobRecord,
    KnowledgeRecord,
    MemoryEvidenceLinkRecord,
    new_id,
    utc_now,
)
from eagle.domain.enums import CandidateState, IndexJobOperation, IndexJobState, KnowledgeStatus
from eagle.knowledge.revalidation import RevalidationService


class KnowledgeService:
    def commit(self, session, candidate: CandidateRecord) -> KnowledgeRecord:
        evidence = list(
            session.scalars(
                select(EvidenceRecord)
                .where(EvidenceRecord.candidate_id == candidate.id)
                .order_by(EvidenceRecord.created_at)
            )
        )
        if not evidence:
            raise RuntimeError("A committed knowledge item must have evidence")

        content = candidate.candidate_value_json
        predecessor = RevalidationService.find_predecessor(session, candidate)
        knowledge_id = new_id()
        knowledge = KnowledgeRecord(
            id=knowledge_id,
            lineage_id=predecessor.lineage_id if predecessor is not None else knowledge_id,
            user_id=candidate.user_id,
            knowledge_type=str(content["type"]),
            retrieval_text=json.dumps(content, ensure_ascii=False, sort_keys=True),
            content_json=content,
            scene_json=candidate.scene_json,
            environment_fingerprint=str(evidence[-1].condition_json["environment"]),
            confidence=candidate.confidence,
            version=predecessor.version + 1 if predecessor is not None else 1,
            parent_version_id=predecessor.id if predecessor is not None else None,
            status=KnowledgeStatus.ACTIVE.value,
        )
        session.add(knowledge)
        session.flush()
        session.add_all(
            [
                MemoryEvidenceLinkRecord(
                    memory_kind="K",
                    memory_id=knowledge.id,
                    evidence_id=item.id,
                )
                for item in evidence
            ]
        )
        session.add(
            IndexJobRecord(
                index_key=f"UPSERT:K:{knowledge.id}:{knowledge.version}",
                memory_kind="K",
                memory_id=knowledge.id,
                operation=IndexJobOperation.UPSERT.value,
                state=IndexJobState.PENDING.value,
            )
        )
        candidate.state = CandidateState.COMMITTED.value
        candidate.updated_at = utc_now()
        if predecessor is not None:
            RevalidationService.complete(session, predecessor, knowledge.environment_fingerprint)
        return knowledge

    @staticmethod
    def revoke_for_candidate(session, candidate: CandidateRecord) -> int:
        knowledge_items = list(
            session.scalars(
                select(KnowledgeRecord)
                .join(
                    MemoryEvidenceLinkRecord,
                    MemoryEvidenceLinkRecord.memory_id == KnowledgeRecord.id,
                )
                .join(EvidenceRecord, EvidenceRecord.id == MemoryEvidenceLinkRecord.evidence_id)
                .where(
                    MemoryEvidenceLinkRecord.memory_kind == "K",
                    EvidenceRecord.candidate_id == candidate.id,
                    KnowledgeRecord.status == KnowledgeStatus.ACTIVE.value,
                )
            )
        )
        for knowledge in knowledge_items:
            knowledge.status = KnowledgeStatus.REVOKED.value
            knowledge.updated_at = utc_now()
        return len(knowledge_items)
