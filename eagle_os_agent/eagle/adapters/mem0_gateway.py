from dataclasses import dataclass

from eagle.adapters.kylin.capabilities import (
    REQUIRED_FOR_GATEWAY,
    probe_vector_capabilities,
)


def _client_backend(memory, attr: str) -> str:
    model = getattr(memory, attr, None)
    client = getattr(model, "client", None)
    return getattr(client, "backend", "unknown")


@dataclass(frozen=True)
class IndexWriteResult:
    memory_id: str
    duplicate_ids: tuple[str, ...] = ()


class Mem0Gateway:
    def __init__(self, memory):
        self.memory = memory
        if memory.config.embedder.provider != "kylin":
            raise RuntimeError("EAGLE requires the kylin embedder provider")
        if memory.config.vector_store.provider != "kylin":
            raise RuntimeError("EAGLE requires the kylin vector store provider")
        if memory.config.llm.provider != "noop":
            raise RuntimeError("EAGLE requires the noop LLM provider")
        self.capability_report = probe_vector_capabilities(
            memory.vector_store.client,
            dimension=memory.vector_store.embedding_model_dims,
            metric=memory.vector_store.distance_metric,
        )
        self.filter_capabilities = self.capability_report.supported
        missing = REQUIRED_FOR_GATEWAY - self.filter_capabilities
        if missing:
            raise RuntimeError(f"Kylin vector store is missing required capabilities: {sorted(missing)}")

    @property
    def backend(self) -> dict:
        """Honest backend report: which clients are actually backing the gateway.

        Values are the clients' ``backend`` tags ("shim", "real", or
        "shim(fallback: <Exc>)"); harnesses/reports must surface this rather
        than assuming the real SDK was used.
        """
        return {
            "embedding": _client_backend(self.memory, "embedding_model"),
            "vector": _client_backend(self.memory, "vector_store"),
        }

    def upsert_knowledge(self, knowledge, index_key: str) -> IndexWriteResult:
        existing = self.find_by_index_key(knowledge.user_id, index_key)
        if existing:
            canonical_id = min(row["id"] for row in existing)
            duplicate_ids = tuple(sorted(row["id"] for row in existing if row["id"] != canonical_id))
            return IndexWriteResult(canonical_id, duplicate_ids)

        response = self.memory.add(
            [{"role": "user", "content": knowledge.retrieval_text}],
            user_id=knowledge.user_id,
            metadata={
                "index_key": index_key,
                "memory_kind": "K",
                "eagle_memory_id": knowledge.id,
                "knowledge_type": knowledge.knowledge_type,
                "status": knowledge.status,
                "version": knowledge.version,
            },
            infer=False,
        )
        results = response["results"]
        if len(results) != 1:
            raise RuntimeError(f"Mem0 raw insert returned {len(results)} results; expected one")
        return IndexWriteResult(results[0]["id"])

    def search_knowledge(
        self,
        *,
        query: str,
        user_id: str,
        limit: int,
        eligible_memory_ids: tuple[str, ...],
    ) -> list[dict]:
        if not eligible_memory_ids:
            return []
        response = self.memory.search(
            query=query,
            filters={
                "user_id": user_id,
                "memory_kind": "K",
                "id": {"in": list(eligible_memory_ids)},
            },
            top_k=limit,
            threshold=0,
        )
        return list(response["results"])

    def embedding_ready(self) -> bool:
        return self.memory.embedding_model.healthcheck()

    def vector_ready(self) -> bool:
        return self.memory.vector_store.healthcheck()

    def delete(self, memory_id: str) -> None:
        if self.memory.get(memory_id) is not None:
            self.memory.delete(memory_id=memory_id)

    def delete_knowledge(self, knowledge, target_mem0_id: str | None) -> None:
        existing = self.find_by_index_key(
            knowledge.user_id,
            f"UPSERT:K:{knowledge.id}:{knowledge.version}",
        )
        memory_ids = {row["id"] for row in existing}
        if target_mem0_id is not None:
            memory_ids.add(target_mem0_id)
        for memory_id in sorted(memory_ids):
            self.delete(memory_id)

    def find_by_index_key(self, user_id: str, index_key: str) -> list[dict]:
        response = self.memory.get_all(
            filters={"user_id": user_id, "index_key": index_key},
            top_k=1000,
        )
        return list(response["results"])
