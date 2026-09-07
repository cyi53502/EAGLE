from dataclasses import asdict, dataclass

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from eagle.db.orm import IndexJobRecord, KnowledgeRecord
from eagle.domain.enums import IndexJobOperation, IndexJobState, KnowledgeStatus


@dataclass(frozen=True)
class HealthReport:
    status: str
    database_ready: bool
    mem0_ready: bool
    kylin_embedding_ready: bool
    kylin_vector_ready: bool
    pending_jobs: int | None
    failed_jobs: int | None
    reconciliation_required: bool
    errors: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


class HealthService:
    def __init__(self, session_factory, gateway):
        self.session_factory = session_factory
        self.gateway = gateway

    def check(self) -> HealthReport:
        errors = []
        database_ready = True
        pending_jobs = None
        failed_jobs = None
        reconciliation_required = True
        try:
            with self.session_factory() as session:
                session.execute(text("SELECT 1"))
                jobs = list(session.scalars(select(IndexJobRecord)))
                knowledge = list(session.scalars(select(KnowledgeRecord)))
            pending_jobs = sum(job.state == IndexJobState.PENDING.value for job in jobs)
            failed_jobs = sum(job.state == IndexJobState.FAILED.value for job in jobs)
            reconciliation_required = self._reconciliation_required(knowledge, jobs)
        except SQLAlchemyError:
            database_ready = False
            errors.append("database_check_failed")

        embedding_ready = self._component_ready("kylin_embedding", self.gateway.embedding_ready, errors)
        vector_ready = self._component_ready("kylin_vector", self.gateway.vector_ready, errors)
        if database_ready and vector_ready:
            try:
                reconciliation_required = reconciliation_required or self._external_index_drift(
                    knowledge,
                    jobs,
                )
            except Exception:  # noqa: BLE001 - vector SDK health boundary
                reconciliation_required = True
                errors.append("vector_reconciliation_check_failed")
        mem0_ready = embedding_ready and vector_ready
        healthy = database_ready and mem0_ready and failed_jobs == 0 and not reconciliation_required
        return HealthReport(
            status="ok" if healthy else "degraded",
            database_ready=database_ready,
            mem0_ready=mem0_ready,
            kylin_embedding_ready=embedding_ready,
            kylin_vector_ready=vector_ready,
            pending_jobs=pending_jobs,
            failed_jobs=failed_jobs,
            reconciliation_required=reconciliation_required,
            errors=tuple(errors),
        )

    @staticmethod
    def _component_ready(name, check, errors: list[str]) -> bool:
        try:
            return bool(check())
        except Exception:  # noqa: BLE001 - provider health boundary
            errors.append(f"{name}_check_failed")
            return False

    def _external_index_drift(self, knowledge, jobs) -> bool:
        active_jobs = {
            (job.operation, job.memory_id)
            for job in jobs
            if job.state in {IndexJobState.PENDING.value, IndexJobState.RUNNING.value}
        }
        for item in knowledge:
            if (
                item.status == KnowledgeStatus.ACTIVE.value and (IndexJobOperation.UPSERT.value, item.id) in active_jobs
            ) or (
                item.status == KnowledgeStatus.FORGETTING.value
                and (IndexJobOperation.DELETE.value, item.id) in active_jobs
            ):
                continue
            rows = self.gateway.find_by_index_key(
                item.user_id,
                f"UPSERT:K:{item.id}:{item.version}",
            )
            indexed_ids = {row["id"] for row in rows}
            if item.status == KnowledgeStatus.ACTIVE.value:
                if indexed_ids != {item.mem0_id}:
                    return True
            elif (
                item.status
                in {
                    KnowledgeStatus.FORGETTING.value,
                    KnowledgeStatus.FORGOTTEN.value,
                }
                and indexed_ids
            ):
                return True
        return False

    @staticmethod
    def _reconciliation_required(knowledge, jobs) -> bool:
        if any(job.state == IndexJobState.FAILED.value for job in jobs):
            return True
        active_jobs = {
            (job.operation, job.memory_id)
            for job in jobs
            if job.state in {IndexJobState.PENDING.value, IndexJobState.RUNNING.value}
        }
        for item in knowledge:
            if (
                item.status == KnowledgeStatus.ACTIVE.value
                and item.mem0_id is None
                and (IndexJobOperation.UPSERT.value, item.id) not in active_jobs
            ):
                return True
            if (
                item.status == KnowledgeStatus.FORGETTING.value
                and (IndexJobOperation.DELETE.value, item.id) not in active_jobs
            ):
                return True
            if item.status == KnowledgeStatus.FORGOTTEN.value and item.mem0_id is not None:
                return True
        return False
