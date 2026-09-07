from sqlalchemy import select

from eagle.db.orm import (
    CandidateRecord,
    EvidenceRecord,
    MemoryEvidenceLinkRecord,
    PreferenceRecord,
    new_id,
    utc_now,
)
from eagle.domain.enums import CandidateState, PreferenceHardness, PreferenceStatus


class PreferenceService:
    def commit(self, session, candidate: CandidateRecord) -> PreferenceRecord:
        existing = list(
            session.scalars(
                select(PreferenceRecord).where(
                    PreferenceRecord.user_id == candidate.user_id,
                    PreferenceRecord.preference_key == candidate.candidate_key,
                    PreferenceRecord.scene_json == candidate.scene_json,
                    PreferenceRecord.status == PreferenceStatus.ACTIVE.value,
                )
            )
        )

        parent = existing[0] if existing else None
        if parent is not None:
            parent.status = PreferenceStatus.REVOKED.value
            parent.updated_at = utc_now()

        preference_id = new_id()
        preference = PreferenceRecord(
            id=preference_id,
            lineage_id=parent.lineage_id if parent is not None else preference_id,
            user_id=candidate.user_id,
            preference_key=candidate.candidate_key,
            preference_value_json=candidate.candidate_value_json,
            hardness=candidate.preference_hardness or PreferenceHardness.SOFT.value,
            scene_json=candidate.scene_json,
            confidence=candidate.confidence,
            version=parent.version + 1 if parent is not None else 1,
            parent_version_id=parent.id if parent is not None else None,
            status=PreferenceStatus.ACTIVE.value,
            authorization_state="EXPLICIT" if candidate.explicit_user_statement else "INFERRED",
        )
        session.add(preference)
        session.flush()

        self._link_evidence(session, candidate, preference.id)
        candidate.state = CandidateState.COMMITTED.value
        candidate.updated_at = utc_now()
        return preference

    @staticmethod
    def revoke_for_candidate(session, candidate: CandidateRecord) -> int:
        preferences = list(
            session.scalars(
                select(PreferenceRecord)
                .join(
                    MemoryEvidenceLinkRecord,
                    MemoryEvidenceLinkRecord.memory_id == PreferenceRecord.id,
                )
                .join(EvidenceRecord, EvidenceRecord.id == MemoryEvidenceLinkRecord.evidence_id)
                .where(
                    MemoryEvidenceLinkRecord.memory_kind == "P",
                    EvidenceRecord.candidate_id == candidate.id,
                    PreferenceRecord.status == PreferenceStatus.ACTIVE.value,
                )
            )
        )
        for preference in preferences:
            preference.status = PreferenceStatus.REVOKED.value
            preference.updated_at = utc_now()
        return len(preferences)

    @staticmethod
    def _link_evidence(session, candidate: CandidateRecord, preference_id: str):
        evidence = session.scalars(select(EvidenceRecord).where(EvidenceRecord.candidate_id == candidate.id))
        links = [
            MemoryEvidenceLinkRecord(
                memory_kind="P",
                memory_id=preference_id,
                evidence_id=item.id,
            )
            for item in evidence
        ]
        if not links:
            raise RuntimeError("A committed preference must have evidence")
        session.add_all(links)
