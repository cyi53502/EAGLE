from sqlalchemy import select

from eagle.db.orm import PKVisibilityRecord, utc_now


class ConflictService:
    def mask_knowledge(self, session, *, preference_id: str, knowledge_id: str, reason: str):
        visibility = session.scalar(
            select(PKVisibilityRecord).where(
                PKVisibilityRecord.preference_version_id == preference_id,
                PKVisibilityRecord.knowledge_version_id == knowledge_id,
            )
        )
        if visibility is None:
            visibility = PKVisibilityRecord(
                preference_version_id=preference_id,
                knowledge_version_id=knowledge_id,
                reason=reason,
                active=True,
            )
            session.add(visibility)
        else:
            visibility.reason = reason
            visibility.active = True
            visibility.updated_at = utc_now()
        return visibility

    def unmask_preference(self, session, preference_id: str) -> int:
        rows = list(
            session.scalars(
                select(PKVisibilityRecord).where(
                    PKVisibilityRecord.preference_version_id == preference_id,
                    PKVisibilityRecord.active.is_(True),
                )
            )
        )
        for row in rows:
            row.active = False
            row.updated_at = utc_now()
        return len(rows)
