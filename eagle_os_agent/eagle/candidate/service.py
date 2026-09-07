import hashlib
import json

from sqlalchemy import distinct, func, select

from eagle.attribution.base import AttributionResult
from eagle.db.orm import CandidateRecord, EpisodeRecord, EvidenceRecord, KnowledgeRecord, utc_now
from eagle.domain.enums import CandidateState, CandidateType, EvidenceDirection, KnowledgeStatus
from eagle.knowledge.revalidation import RevalidationService


class CandidateService:
    def apply_evidence(self, session, episode: EpisodeRecord, result: AttributionResult) -> CandidateRecord:
        identity = self._identity(episode.user_id, result)
        candidate = session.scalar(select(CandidateRecord).where(CandidateRecord.candidate_identity == identity))
        if candidate is None:
            candidate = CandidateRecord(
                candidate_identity=identity,
                user_id=episode.user_id,
                candidate_type=result.candidate_type.value,
                candidate_key=result.key,
                candidate_value_json=result.value,
                scene_json=result.scene.normalized(),
                preference_hardness=(
                    result.preference_hardness.value if result.preference_hardness is not None else None
                ),
                state=CandidateState.PENDING.value,
            )
            session.add(candidate)
            session.flush()

        evidence = session.scalar(
            select(EvidenceRecord).where(
                EvidenceRecord.candidate_id == candidate.id,
                EvidenceRecord.episode_id == episode.id,
                EvidenceRecord.evidence_type == result.candidate_type.value,
                EvidenceRecord.direction == result.direction.value,
                EvidenceRecord.attribution_reason == result.reason,
            )
        )
        if evidence is None:
            evidence = EvidenceRecord(
                episode_id=episode.id,
                candidate_id=candidate.id,
                evidence_type=result.candidate_type.value,
                direction=result.direction.value,
                strength=result.strength.value,
                contribution=result.contribution,
                confidence_delta=result.confidence_delta,
                condition_json={
                    "scene": result.scene.normalized(),
                    "environment": episode.environment_fingerprint,
                },
                attribution_reason=result.reason,
            )
            session.add(evidence)

            if result.direction is EvidenceDirection.POSITIVE:
                candidate.positive_evidence += result.contribution
            elif result.direction is EvidenceDirection.NEGATIVE:
                candidate.negative_evidence += result.contribution

            candidate.independent_choices += result.independent_choice_delta
            candidate.same_condition_success += result.same_condition_success_delta
            candidate.confidence = min(1.0, max(0.0, candidate.confidence + result.confidence_delta))
            candidate.explicit_user_statement = candidate.explicit_user_statement or result.explicit
            candidate.user_confirmed = candidate.user_confirmed or result.user_confirmed
            if result.preference_hardness is not None:
                candidate.preference_hardness = result.preference_hardness.value
            candidate.updated_at = utc_now()
            session.flush()

        candidate.distinct_sessions = session.scalar(
            select(func.count(distinct(EpisodeRecord.session_id)))
            .select_from(EvidenceRecord)
            .join(EpisodeRecord, EpisodeRecord.id == EvidenceRecord.episode_id)
            .where(EvidenceRecord.candidate_id == candidate.id)
        )
        return candidate

    @staticmethod
    def _identity(user_id: str, result: AttributionResult) -> str:
        source = "|".join(
            [
                user_id,
                result.candidate_type.value,
                result.key,
                json.dumps(result.value, sort_keys=True, separators=(",", ":")),
                json.dumps(result.scene.normalized(), sort_keys=True, separators=(",", ":")),
            ]
        )
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    @staticmethod
    def mark_preference_conflict(session, candidate: CandidateRecord):
        if candidate.candidate_type != CandidateType.PREFERENCE.value:
            return

        from eagle.db.orm import PreferenceRecord

        active = session.scalars(
            select(PreferenceRecord).where(
                PreferenceRecord.user_id == candidate.user_id,
                PreferenceRecord.preference_key == candidate.candidate_key,
                PreferenceRecord.status == "ACTIVE",
            )
        )
        candidate.has_unresolved_conflict = (
            any(
                preference.scene_json == candidate.scene_json
                and preference.preference_value_json != candidate.candidate_value_json
                for preference in active
            )
            and not candidate.explicit_user_statement
        )

    @staticmethod
    def mark_knowledge_conflict(session, candidate: CandidateRecord):
        if candidate.candidate_type != CandidateType.KNOWLEDGE.value:
            return
        content = candidate.candidate_value_json
        if content.get("type") != "WORKFLOW":
            candidate.has_unresolved_conflict = False
            return

        active = list(
            session.scalars(
                select(KnowledgeRecord).where(
                    KnowledgeRecord.user_id == candidate.user_id,
                    KnowledgeRecord.knowledge_type == "WORKFLOW",
                    KnowledgeRecord.status == KnowledgeStatus.ACTIVE.value,
                )
            )
        )
        conflicts = [
            knowledge
            for knowledge in active
            if knowledge.scene_json == candidate.scene_json
            and knowledge.environment_fingerprint == content["when"]["environment"]
            and knowledge.content_json["when"] == content["when"]
            and knowledge.content_json["action"] != content["action"]
        ]
        conflict_confirmed = candidate.user_confirmed or candidate.same_condition_success >= 2
        candidate.has_unresolved_conflict = bool(conflicts) and not conflict_confirmed
        for knowledge in conflicts:
            RevalidationService.mark_required(
                session,
                knowledge,
                target_environment=content["when"]["environment"],
                reason="K_K_CONFLICT",
            )
            if conflict_confirmed:
                knowledge.status = KnowledgeStatus.NEEDS_REVALIDATION.value
                knowledge.updated_at = utc_now()
