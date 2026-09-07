from datetime import timedelta

from sqlalchemy import select

from eagle.db.orm import IndexJobRecord, KnowledgeRecord, utc_now
from eagle.domain.enums import IndexJobOperation, IndexJobState, KnowledgeStatus


class ReconciliationService:
    def __init__(self, session_factory, gateway, lease_seconds: int = 300):
        self.session_factory = session_factory
        self.gateway = gateway
        self.lease_seconds = lease_seconds

    def reconcile(self) -> int:
        with self.session_factory() as session:
            knowledge_items = list(session.scalars(select(KnowledgeRecord)))

        indexed_rows = {
            item.id: self.gateway.find_by_index_key(
                item.user_id,
                f"UPSERT:K:{item.id}:{item.version}",
            )
            for item in knowledge_items
        }

        changed = 0
        with self.session_factory.begin() as session:
            changed += self._reset_stale_running_jobs(session)
            for snapshot in knowledge_items:
                knowledge = session.get(KnowledgeRecord, snapshot.id)
                rows = indexed_rows[knowledge.id]
                indexed_ids = {row["id"] for row in rows}

                if knowledge.status == KnowledgeStatus.ACTIVE.value:
                    if not indexed_ids:
                        if knowledge.mem0_id is not None:
                            knowledge.mem0_id = None
                            changed += 1
                        changed += self._ensure_job(
                            session,
                            index_key=f"UPSERT:K:{knowledge.id}:{knowledge.version}",
                            knowledge=knowledge,
                            operation=IndexJobOperation.UPSERT.value,
                        )
                        continue

                    canonical_id = knowledge.mem0_id if knowledge.mem0_id in indexed_ids else min(indexed_ids)
                    if knowledge.mem0_id != canonical_id:
                        knowledge.mem0_id = canonical_id
                        changed += 1
                    upsert_job = session.scalar(
                        select(IndexJobRecord).where(
                            IndexJobRecord.index_key == f"UPSERT:K:{knowledge.id}:{knowledge.version}"
                        )
                    )
                    if upsert_job is not None and upsert_job.state != IndexJobState.DONE.value:
                        upsert_job.state = IndexJobState.DONE.value
                        upsert_job.last_error = None
                        upsert_job.locked_at = None
                        changed += 1
                    for duplicate_id in sorted(indexed_ids - {canonical_id}):
                        changed += self._ensure_job(
                            session,
                            index_key=f"DELETE_DUPLICATE:{duplicate_id}",
                            knowledge=knowledge,
                            operation=IndexJobOperation.DELETE_DUPLICATE.value,
                            target_mem0_id=duplicate_id,
                        )

                elif knowledge.status == KnowledgeStatus.FORGETTING.value or (
                    knowledge.status == KnowledgeStatus.FORGOTTEN.value
                    and (indexed_ids or knowledge.mem0_id is not None)
                ):
                    changed += self._ensure_job(
                        session,
                        index_key=f"DELETE:K:{knowledge.id}:{knowledge.version}",
                        knowledge=knowledge,
                        operation=IndexJobOperation.DELETE.value,
                        target_mem0_id=knowledge.mem0_id,
                    )
        return changed

    def _reset_stale_running_jobs(self, session) -> int:
        lease_deadline = utc_now() - timedelta(seconds=self.lease_seconds)
        stale_jobs = list(
            session.scalars(
                select(IndexJobRecord).where(
                    IndexJobRecord.state == IndexJobState.RUNNING.value,
                    IndexJobRecord.locked_at < lease_deadline,
                )
            )
        )
        for job in stale_jobs:
            job.state = IndexJobState.PENDING.value
            job.locked_at = None
            job.available_at = utc_now()
            job.updated_at = utc_now()
        return len(stale_jobs)

    @staticmethod
    def _ensure_job(
        session,
        *,
        index_key: str,
        knowledge: KnowledgeRecord,
        operation: str,
        target_mem0_id: str | None = None,
    ) -> int:
        existing = session.scalar(select(IndexJobRecord).where(IndexJobRecord.index_key == index_key))
        if existing is None:
            session.add(
                IndexJobRecord(
                    index_key=index_key,
                    memory_kind="K",
                    memory_id=knowledge.id,
                    operation=operation,
                    state=IndexJobState.PENDING.value,
                    target_mem0_id=target_mem0_id,
                )
            )
            return 1
        if existing.state == IndexJobState.DONE.value:
            existing.state = IndexJobState.PENDING.value
            existing.last_error = None
            existing.locked_at = None
            existing.target_mem0_id = target_mem0_id
            return 1
        return 0
