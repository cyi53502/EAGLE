from dataclasses import dataclass
from typing import Optional

import pytest

from mem0 import Memory
from mem0.exceptions import LLMError


class EmbeddingClient:
    def __init__(self):
        self.calls = []

    def embed(self, *, text, action):
        self.calls.append((text, action))
        return [float(len(text)), 1.0]

    def embed_batch(self, *, texts, action):
        return [self.embed(text=text, action=action) for text in texts]

    def healthcheck(self):
        return True


@dataclass
class Row:
    id: str
    score: float
    payload: dict
    vector: Optional[list] = None


class VectorClient:
    def __init__(self):
        self.collections = {}

    def ensure_collection(self, *, name, dimension, metric):
        self.collections.setdefault(name, {})

    def upsert(self, *, collection, vectors, payloads, ids):
        for vector, payload, vector_id in zip(vectors, payloads, ids):
            self.collections[collection][vector_id] = Row(vector_id, 0.0, payload, vector)

    def search(self, *, collection, vector, limit, filters):
        rows = [
            row
            for row in self.collections[collection].values()
            if all(row.payload.get(key) == value for key, value in filters.items())
        ]
        return rows[:limit]

    def delete(self, *, collection, vector_id):
        del self.collections[collection][vector_id]

    def get(self, *, collection, vector_id, include_vector):
        return self.collections[collection].get(vector_id)

    def list_collections(self):
        return list(self.collections)

    def delete_collection(self, *, name):
        del self.collections[name]

    def collection_info(self, *, name):
        return {"count": len(self.collections[name])}

    def list(self, *, collection, filters, limit):
        rows = [
            row
            for row in self.collections[collection].values()
            if all(row.payload.get(key) == value for key, value in filters.items())
        ]
        return rows[:limit]

    def healthcheck(self, **_kwargs):
        return True


def test_memory_raw_add_search_delete_uses_kylin_providers(tmp_path, monkeypatch):
    monkeypatch.setattr("mem0.utils.spacy_models._load_failed_full", True)
    monkeypatch.setattr("mem0.utils.spacy_models._load_failed_lemma", True)
    vector_client = VectorClient()
    embedding_client = EmbeddingClient()
    memory = Memory.from_config(
        {
            "embedder": {
                "provider": "kylin",
                "config": {"embedding_dims": 2, "client": embedding_client},
            },
            "vector_store": {
                "provider": "kylin",
                "config": {
                    "collection_name": "eagle_memories",
                    "embedding_model_dims": 2,
                    "distance_metric": "vendor-cosine",
                    "score_semantics": "cosine_distance",
                    "client": vector_client,
                },
            },
            "llm": {"provider": "noop", "config": {}},
            "history_db_path": str(tmp_path / "history.db"),
        }
    )

    added = memory.add(
        [{"role": "user", "content": "Use LibreOffice after WPS fails"}],
        user_id="u1",
        metadata={"memory_kind": "K", "eagle_memory_id": "k1"},
        infer=False,
    )
    memory_id = added["results"][0]["id"]
    assert embedding_client.calls == [("Use LibreOffice after WPS fails", "add")]

    found = memory.search(query="LibreOffice", filters={"user_id": "u1"}, top_k=5)
    assert found["results"][0]["metadata"]["eagle_memory_id"] == "k1"

    with pytest.raises(LLMError, match="infer=False"):
        memory.add("must fail", user_id="u1", infer=True)

    memory.delete(memory_id)
    assert memory.get(memory_id) is None
