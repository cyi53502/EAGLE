from sqlalchemy import select

from eagle.db.orm import (
    EvidenceRecord,
    KnowledgeRecord,
    MemoryEvidenceLinkRecord,
    PKVisibilityRecord,
    PreferenceRecord,
)
from eagle.domain.constraints import PlannerContext
from eagle.domain.enums import KnowledgeStatus, PreferenceHardness, PreferenceStatus
from eagle.domain.scene import Scene
from eagle.knowledge.revalidation import RevalidationService
from eagle.preference.compiler import PreferenceCompiler
from eagle.preference.resolver import PreferenceResolver


class PackService:
    def __init__(self, session_factory, gateway):
        self.session_factory = session_factory
        self.gateway = gateway
        self.resolver = PreferenceResolver()
        self.compiler = PreferenceCompiler()

    def build(
        self,
        *,
        query: str,
        user_id: str,
        scene: Scene,
        environment_fingerprint: str,
        top_k: int = 5,
    ) -> PlannerContext:
        with self.session_factory.begin() as session:
            preferences = list(
                session.scalars(
                    select(PreferenceRecord).where(
                        PreferenceRecord.user_id == user_id,
                        PreferenceRecord.status == PreferenceStatus.ACTIVE.value,
                    )
                )
            )
            resolved = self.resolver.resolve(preferences, scene)
            constraints = self.compiler.compile(resolved)
            active_knowledge = list(
                session.scalars(
                    select(KnowledgeRecord).where(
                        KnowledgeRecord.user_id == user_id,
                        KnowledgeRecord.status == KnowledgeStatus.ACTIVE.value,
                    )
                )
            )
            current_scene = scene.normalized()
            scene_knowledge = [
                knowledge for knowledge in active_knowledge if self._scene_matches(knowledge.scene_json, current_scene)
            ]
            for knowledge in scene_knowledge:
                if knowledge.environment_fingerprint != environment_fingerprint:
                    RevalidationService.mark_required(
                        session,
                        knowledge,
                        target_environment=environment_fingerprint,
                        reason="ENVIRONMENT_DRIFT",
                    )
            applicable_knowledge = [
                knowledge
                for knowledge in scene_knowledge
                if knowledge.environment_fingerprint == environment_fingerprint
            ]
            eligible_memory_ids = tuple(
                knowledge.mem0_id for knowledge in applicable_knowledge if knowledge.mem0_id is not None
            )
            if not eligible_memory_ids:
                return PlannerContext(constraints=constraints, knowledge=())
            search_results = self.gateway.search_knowledge(
                query=query,
                user_id=user_id,
                limit=top_k * 4,
                eligible_memory_ids=eligible_memory_ids,
            )

            result_by_knowledge_id = {}
            for result in search_results:
                metadata = result.get("metadata", {})
                knowledge_id = metadata.get("eagle_memory_id")
                if knowledge_id is not None and knowledge_id not in result_by_knowledge_id:
                    result_by_knowledge_id[knowledge_id] = result

            if not result_by_knowledge_id:
                return PlannerContext(constraints=constraints, knowledge=())

            knowledge_by_id = {item.id: item for item in applicable_knowledge}
            preference_ids = [preference.id for preference in resolved]
            blocked = set()
            if preference_ids:
                blocked = set(
                    session.scalars(
                        select(PKVisibilityRecord.knowledge_version_id).where(
                            PKVisibilityRecord.preference_version_id.in_(preference_ids),
                            PKVisibilityRecord.active.is_(True),
                        )
                    )
                )

            selected = []
            for knowledge_id, result in result_by_knowledge_id.items():
                knowledge = knowledge_by_id.get(knowledge_id)
                if knowledge is None or knowledge_id in blocked:
                    continue
                selected.append((knowledge, result))

            soft_tools = {
                preference.preference_value_json["tool"]
                for preference in resolved
                if preference.hardness == PreferenceHardness.SOFT.value
                and preference.preference_key == "preferred_tool"
            }
            if soft_tools:
                selected.sort(
                    key=lambda item: item[0].content_json.get("action", {}).get("fallback_to") in soft_tools,
                    reverse=True,
                )
            selected = selected[:top_k]

            evidence = self._evidence(session, [knowledge.id for knowledge, _ in selected])
            return PlannerContext(
                constraints=constraints,
                knowledge=tuple(
                    {
                        "id": knowledge.id,
                        "type": knowledge.knowledge_type,
                        "content": knowledge.content_json,
                        "score": result.get("score"),
                    }
                    for knowledge, result in selected
                ),
                evidence=tuple(evidence),
            )

    @staticmethod
    def _scene_matches(memory_scene: dict, current_scene: dict) -> bool:
        return all(current_scene.get(key) == value for key, value in memory_scene.items())

    @staticmethod
    def _evidence(session, memory_ids: list[str]) -> list[dict]:
        if not memory_ids:
            return []
        rows = session.execute(
            select(MemoryEvidenceLinkRecord.memory_id, EvidenceRecord)
            .join(EvidenceRecord, EvidenceRecord.id == MemoryEvidenceLinkRecord.evidence_id)
            .where(
                MemoryEvidenceLinkRecord.memory_kind == "K",
                MemoryEvidenceLinkRecord.memory_id.in_(memory_ids),
            )
        )
        return [
            {
                "memory_id": memory_id,
                "episode_id": item.episode_id,
                "reason": item.attribution_reason,
                "contribution": item.contribution,
            }
            for memory_id, item in rows
        ]
