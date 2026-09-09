from types import SimpleNamespace

import pytest
from eagle.db.orm import CandidateRecord, EpisodeRecord, EvidenceRecord, IndexJobRecord, KnowledgeRecord
from sqlalchemy import select
from test_governance import episode

from eagle.adapters.mem0_gateway import Mem0Gateway
from eagle.forgetting.service import ForgettingService
from eagle.governance import GovernanceService
from eagle.outbox.worker import IndexWorker


class CapabilityClient:
    def __init__(self):
        self.collections = {}

    def ensure_collection(self, *, name, dimension, metric):
        self.collections.setdefault(name, {})

    def upsert(self, *, collection, vectors, payloads, ids):
        for vector, payload, vector_id in zip(vectors, payloads, ids):
            self.collections[collection][vector_id] = SimpleNamespace(
                id=vector_id,
                vector=vector,
                payload=payload | {"__id__": vector_id},
            )

    def get(self, *, collection, vector_id, include_vector):
        return self.collections[collection].get(vector_id)

    def search(self, *, collection, vector, limit, filters):
        rows = [row for row in self.collections[collection].values() if self._matches(row.payload, filters)]
        return rows[:limit]

    def list(self, *, collection, filters, limit):
        rows = [row for row in self.collections[collection].values() if self._matches(row.payload, filters)]
        return rows[:limit]

    def delete(self, *, collection, vector_id):
        self.collections[collection].pop(vector_id, None)

    def delete_collection(self, *, name):
        self.collections.pop(name, None)

    @staticmethod
    def _matches(payload, filters):
        row_id = payload.get("__id__")
        for key, expected in filters.items():
            if key == "$not":
                if any(CapabilityClient._matches(payload, condition) for condition in expected):
                    return False
            elif key == "id":
                if isinstance(expected, dict) and "in" in expected:
                    if row_id not in expected["in"]:
                        return False
                elif isinstance(expected, dict) and "nin" in expected:
                    if row_id in expected["nin"]:
                        return False
                elif row_id != expected:
                    return False
            elif isinstance(expected, dict) and "in" in expected:
                if payload.get(key) not in expected["in"]:
                    return False
            elif isinstance(expected, dict) and "nin" in expected:
                if payload.get(key) in expected["nin"]:
                    return False
            elif isinstance(expected, dict) and "ne" in expected:
                if payload.get(key) == expected["ne"]:
                    return False
            elif payload.get(key) != expected:
                return False
        return True


class FakeMemory:
    def __init__(self):
        self.config = SimpleNamespace(
            embedder=SimpleNamespace(provider="kylin"),
            vector_store=SimpleNamespace(provider="kylin"),
            llm=SimpleNamespace(provider="noop"),
        )
        self.rows = []
        self.add_calls = []
        self.search_calls = []
        self.vector_store = SimpleNamespace(
            client=CapabilityClient(),
            embedding_model_dims=2,
            distance_metric="cosine_distance",
            healthcheck=lambda: True,
        )
        self.embedding_model = SimpleNamespace(healthcheck=lambda: True)

    def get_all(self, *, filters, top_k):
        rows = [
            row for row in self.rows if all(row.get("metadata", {}).get(key) == value for key, value in filters.items())
        ]
        return {"results": rows[:top_k]}

    def add(self, messages, **kwargs):
        self.add_calls.append((messages, kwargs))
        row = {
            "id": "vector-1",
            "memory": messages[0]["content"],
            "metadata": kwargs["metadata"] | {"user_id": kwargs["user_id"]},
        }
        self.rows.append(row)
        return {"results": [{"id": row["id"]}]}

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return {"results": []}

    def get(self, memory_id):
        return next((row for row in self.rows if row["id"] == memory_id), None)

    def delete(self, memory_id):
        self.rows = [row for row in self.rows if row["id"] != memory_id]


def test_worker_persists_committed_knowledge_with_raw_insert(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(episode(session_id="s1", tool="libreoffice", fallback_from="wps"))
    governance.record_episode(episode(session_id="s2", tool="libreoffice", fallback_from="wps"))
    memory = FakeMemory()

    job_id = IndexWorker(session_factory, Mem0Gateway(memory)).process_next()

    assert job_id is not None
    assert memory.add_calls[0][1]["infer"] is False
    with session_factory() as session:
        knowledge = session.scalar(select(KnowledgeRecord))
        job = session.scalar(select(IndexJobRecord))
    assert knowledge.mem0_id == "vector-1"
    assert job.state == "DONE"


def test_worker_physically_deletes_forgotten_knowledge(session_factory):
    governance = GovernanceService(session_factory)
    first_episode = episode(session_id="s1", tool="libreoffice", fallback_from="wps")
    governance.record_episode(first_episode)
    governance.record_episode(episode(session_id="s2", tool="libreoffice", fallback_from="wps"))
    memory = FakeMemory()
    worker = IndexWorker(session_factory, Mem0Gateway(memory))
    worker.process_next()
    with session_factory() as session:
        knowledge_id = session.scalar(select(KnowledgeRecord.id))

    ForgettingService(session_factory).forget_knowledge(knowledge_id, user_id="u1")
    worker.process_next()

    assert memory.rows == []
    with session_factory() as session:
        knowledge = session.get(KnowledgeRecord, knowledge_id)
        evidence = session.scalar(select(EvidenceRecord))
        candidate = session.scalar(select(CandidateRecord))
        episodes = list(session.scalars(select(EpisodeRecord)))
    assert knowledge.status == "FORGOTTEN"
    assert knowledge.mem0_id is None
    assert knowledge.content_json == {}
    assert knowledge.retrieval_text == ""
    assert evidence is None
    assert candidate is None
    assert all(item.request_text == "" for item in episodes)

    replay = governance.record_episode(first_episode)
    assert replay.candidate_ids == ()
    assert replay.committed_memory_ids == ()
    with session_factory() as session:
        assert len(list(session.scalars(select(KnowledgeRecord)))) == 1
        assert session.scalar(select(EvidenceRecord)) is None


def test_forgetting_before_upsert_never_creates_vector(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(episode(session_id="s1", tool="libreoffice", fallback_from="wps"))
    governance.record_episode(episode(session_id="s2", tool="libreoffice", fallback_from="wps"))
    with session_factory() as session:
        knowledge_id = session.scalar(select(KnowledgeRecord.id))
    ForgettingService(session_factory).forget_knowledge(knowledge_id, user_id="u1")
    memory = FakeMemory()
    worker = IndexWorker(session_factory, Mem0Gateway(memory))

    worker.process_next()
    worker.process_next()

    assert memory.add_calls == []
    with session_factory() as session:
        knowledge = session.get(KnowledgeRecord, knowledge_id)
    assert knowledge.status == "FORGOTTEN"


def test_gateway_rejects_vector_store_without_exact_tenant_filter():
    memory = FakeMemory()
    memory.vector_store.client.list = lambda **_kwargs: []

    try:
        Mem0Gateway(memory)
    except RuntimeError as error:
        assert "required capabilities" in str(error)
    else:
        raise AssertionError("gateway accepted a vector store without exact tenant filtering")


def test_gateway_search_uses_active_vector_id_allowlist():
    memory = FakeMemory()
    gateway = Mem0Gateway(memory)

    gateway.search_knowledge(
        query="fallback",
        user_id="u1",
        limit=5,
        eligible_memory_ids=("vector-a", "vector-b"),
    )

    assert memory.search_calls == [
        {
            "query": "fallback",
            "filters": {
                "user_id": "u1",
                "memory_kind": "K",
                "id": {"in": ["vector-a", "vector-b"]},
            },
            "top_k": 5,
        }
    ]


def test_delete_finds_vector_when_upsert_crashed_before_mem0_id_writeback(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(episode(session_id="s1", tool="libreoffice", fallback_from="wps"))
    governance.record_episode(episode(session_id="s2", tool="libreoffice", fallback_from="wps"))
    memory = FakeMemory()
    gateway = Mem0Gateway(memory)
    with session_factory.begin() as session:
        knowledge = session.scalar(select(KnowledgeRecord))
        job = session.scalar(select(IndexJobRecord))
        gateway.upsert_knowledge(knowledge, job.index_key)
        job.state = "RUNNING"
        knowledge_id = knowledge.id

    ForgettingService(session_factory).forget_knowledge(knowledge_id, user_id="u1")
    IndexWorker(session_factory, gateway).process_next()

    assert memory.rows == []
    with session_factory() as session:
        knowledge = session.get(KnowledgeRecord, knowledge_id)
    assert knowledge.status == "FORGOTTEN"


def test_worker_keeps_canonical_vector_and_schedules_duplicate_cleanup(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(episode(session_id="s1", tool="libreoffice", fallback_from="wps"))
    governance.record_episode(episode(session_id="s2", tool="libreoffice", fallback_from="wps"))
    memory = FakeMemory()
    with session_factory() as session:
        job = session.scalar(select(IndexJobRecord))
        knowledge = session.get(KnowledgeRecord, job.memory_id)
        metadata = {
            "user_id": knowledge.user_id,
            "index_key": job.index_key,
            "eagle_memory_id": knowledge.id,
        }
        memory.rows = [
            {"id": "vector-b", "metadata": metadata},
            {"id": "vector-a", "metadata": metadata},
        ]

    worker = IndexWorker(session_factory, Mem0Gateway(memory))
    worker.process_next()
    with session_factory() as session:
        knowledge = session.scalar(select(KnowledgeRecord))
        duplicate_job = session.scalar(select(IndexJobRecord).where(IndexJobRecord.operation == "DELETE_DUPLICATE"))
    assert knowledge.mem0_id == "vector-a"
    assert duplicate_job.target_mem0_id == "vector-b"

    worker.process_next()

    assert [row["id"] for row in memory.rows] == ["vector-a"]


def test_delete_failure_keeps_forgetting_and_exposes_failed_job(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(episode(session_id="s1", tool="libreoffice", fallback_from="wps"))
    governance.record_episode(episode(session_id="s2", tool="libreoffice", fallback_from="wps"))
    memory = FakeMemory()
    worker = IndexWorker(session_factory, Mem0Gateway(memory), max_retries=1)
    worker.process_next()
    with session_factory() as session:
        knowledge_id = session.scalar(select(KnowledgeRecord.id))
    ForgettingService(session_factory).forget_knowledge(knowledge_id, user_id="u1")

    def fail_delete(memory_id):
        raise RuntimeError("vector service unavailable")

    memory.delete = fail_delete
    with pytest.raises(RuntimeError, match="vector service unavailable"):
        worker.process_next()

    with session_factory() as session:
        knowledge = session.get(KnowledgeRecord, knowledge_id)
        delete_job = session.scalar(select(IndexJobRecord).where(IndexJobRecord.operation == "DELETE"))
    assert knowledge.status == "FORGETTING"
    assert delete_job.state == "FAILED"
    assert delete_job.retry_count == 1
