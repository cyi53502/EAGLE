from sqlalchemy import select

from eagle.db.orm import KnowledgeRecord, KnowledgeRevalidationRecord, utc_now
from eagle.domain.enums import KnowledgeStatus


class RevalidationService:
    @staticmethod
    def mark_required(
        session,
        knowledge: KnowledgeRecord,
        *,
        target_environment: str,
        reason: str,
    ) -> KnowledgeRevalidationRecord:
        request = session.scalar(
            select(KnowledgeRevalidationRecord).where(
                KnowledgeRevalidationRecord.source_knowledge_id == knowledge.id,
                KnowledgeRevalidationRecord.target_environment == target_environment,
                KnowledgeRevalidationRecord.reason == reason,
            )
        )
        if request is None:
            request = KnowledgeRevalidationRecord(
                source_knowledge_id=knowledge.id,
                target_environment=target_environment,
                reason=reason,
                state="PENDING",
            )
            session.add(request)
        return request

    @staticmethod
    def find_predecessor(session, candidate) -> KnowledgeRecord | None:
        content = candidate.candidate_value_json
        if content.get("type") != "WORKFLOW":
            return None
        candidate_condition = {key: value for key, value in content["when"].items() if key != "environment"}
        target_environment = content["when"]["environment"]
        requests = session.scalars(
            select(KnowledgeRevalidationRecord).where(
                KnowledgeRevalidationRecord.target_environment == target_environment,
                KnowledgeRevalidationRecord.state == "PENDING",
            )
        )
        requested_predecessors = []
        for request in requests:
            knowledge = session.get(KnowledgeRecord, request.source_knowledge_id)
            if RevalidationService._matches_predecessor(knowledge, candidate, candidate_condition):
                requested_predecessors.append(knowledge)
        if requested_predecessors:
            return max(
                requested_predecessors,
                key=lambda knowledge: (knowledge.version, knowledge.created_at, knowledge.id),
            )

        rows = session.scalars(
            select(KnowledgeRecord).where(
                KnowledgeRecord.user_id == candidate.user_id,
                KnowledgeRecord.knowledge_type == "WORKFLOW",
                KnowledgeRecord.status.in_([KnowledgeStatus.ACTIVE.value, KnowledgeStatus.NEEDS_REVALIDATION.value]),
            )
        )
        predecessors = [
            knowledge
            for knowledge in rows
            if RevalidationService._matches_predecessor(
                knowledge,
                candidate,
                candidate_condition,
            )
            and knowledge.content_json["action"] == content["action"]
            and knowledge.environment_fingerprint != target_environment
        ]
        if not predecessors:
            return None
        return max(predecessors, key=lambda knowledge: (knowledge.version, knowledge.created_at, knowledge.id))

    @staticmethod
    def complete(session, predecessor: KnowledgeRecord, target_environment: str) -> None:
        requests = session.scalars(
            select(KnowledgeRevalidationRecord).where(
                KnowledgeRevalidationRecord.source_knowledge_id == predecessor.id,
                KnowledgeRevalidationRecord.target_environment == target_environment,
                KnowledgeRevalidationRecord.state == "PENDING",
            )
        )
        for request in requests:
            request.state = "DONE"
            request.updated_at = utc_now()

    @staticmethod
    def _matches_predecessor(knowledge, candidate, candidate_condition) -> bool:
        if knowledge is None or knowledge.user_id != candidate.user_id:
            return False
        if knowledge.knowledge_type != "WORKFLOW" or knowledge.scene_json != candidate.scene_json:
            return False
        return {
            key: value for key, value in knowledge.content_json["when"].items() if key != "environment"
        } == candidate_condition
