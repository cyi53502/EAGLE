from datetime import timedelta

from sqlalchemy import select, text

from eagle.db.orm import IndexJobRecord, KnowledgeRecord, utc_now
from eagle.domain.enums import IndexJobOperation, IndexJobState, KnowledgeStatus
from eagle.forgetting.service import ForgettingService


class IndexWorker:
    def __init__(self, session_factory, gateway, max_retries: int = 3):
        self.session_factory = session_factory
        self.gateway = gateway
        self.max_retries = max_retries

    def process_next(self) -> str | None:
        job_id = self._claim_next()
        if job_id is None:
            return None

        try:
            self._execute(job_id)
        except Exception as error:
            with self.session_factory.begin() as session:
                job = session.get(IndexJobRecord, job_id)
                job.retry_count += 1
                if job.retry_count >= self.max_retries:
                    job.state = IndexJobState.FAILED.value
                else:
                    job.state = IndexJobState.PENDING.value
                    job.available_at = utc_now() + timedelta(seconds=2 ** (job.retry_count - 1))
                job.last_error = str(error)
                job.locked_at = None
                job.updated_at = utc_now()
            raise
        return job_id

    def _claim_next(self) -> str | None:
        with self.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            job = session.scalar(
                select(IndexJobRecord)
                .where(
                    IndexJobRecord.state == IndexJobState.PENDING.value,
                    IndexJobRecord.available_at <= utc_now(),
                )
                .order_by(IndexJobRecord.created_at)
                .limit(1)
            )
            if job is None:
                session.commit()
                return None
            job.state = IndexJobState.RUNNING.value
            job.locked_at = utc_now()
            job.updated_at = utc_now()
            job_id = job.id
            session.commit()
            return job_id

    def _execute(self, job_id: str) -> None:
        duplicate_ids = ()
        with self.session_factory() as session:
            job = session.get(IndexJobRecord, job_id)
            knowledge = session.get(KnowledgeRecord, job.memory_id)
            if job.operation == IndexJobOperation.UPSERT.value:
                if knowledge.status == KnowledgeStatus.ACTIVE.value:
                    result = self.gateway.upsert_knowledge(knowledge, job.index_key)
                    mem0_id = result.memory_id
                    duplicate_ids = result.duplicate_ids
                else:
                    mem0_id = knowledge.mem0_id
            elif job.operation == IndexJobOperation.DELETE.value:
                target_mem0_id = job.target_mem0_id or knowledge.mem0_id
                self.gateway.delete_knowledge(knowledge, target_mem0_id)
                mem0_id = None
            else:
                self.gateway.delete(job.target_mem0_id)
                mem0_id = knowledge.mem0_id

        with self.session_factory.begin() as session:
            job = session.get(IndexJobRecord, job_id)
            knowledge = session.get(KnowledgeRecord, job.memory_id)
            if job.operation != IndexJobOperation.DELETE_DUPLICATE.value:
                knowledge.mem0_id = mem0_id
                if job.operation == IndexJobOperation.DELETE.value:
                    ForgettingService.finalize_knowledge(session, knowledge)
                else:
                    knowledge.updated_at = utc_now()
            for duplicate_id in duplicate_ids:
                index_key = f"DELETE_DUPLICATE:{duplicate_id}"
                existing_cleanup = session.scalar(select(IndexJobRecord).where(IndexJobRecord.index_key == index_key))
                if existing_cleanup is None:
                    session.add(
                        IndexJobRecord(
                            index_key=index_key,
                            memory_kind="K",
                            memory_id=knowledge.id,
                            operation=IndexJobOperation.DELETE_DUPLICATE.value,
                            state=IndexJobState.PENDING.value,
                            target_mem0_id=duplicate_id,
                        )
                    )
            job.state = IndexJobState.DONE.value
            job.last_error = None
            job.locked_at = None
            job.updated_at = utc_now()
