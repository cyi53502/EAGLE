from dataclasses import dataclass
from typing import Optional

from mem0.vector_stores.kylin import KylinVectorStore


@dataclass
class Row:
    id: str
    score: float
    payload: dict
    vector: Optional[list] = None


class FakeClient:
    def __init__(self):
        self.rows = {"v1": Row("v1", 0.0, {"user_id": "u1"}, [1.0, 2.0])}

    def ensure_collection(self, **_kwargs):
        pass

    def search(self, **_kwargs):
        return [Row("v1", 0.25, {"user_id": "u1"})]

    def upsert(self, *, vectors, payloads, ids, **_kwargs):
        for vector, payload, vector_id in zip(vectors, payloads, ids):
            self.rows[vector_id] = Row(vector_id, 0.0, payload, vector)

    def delete(self, *, vector_id, **_kwargs):
        self.rows.pop(vector_id)

    def get(self, *, vector_id, **_kwargs):
        return self.rows.get(vector_id)

    def list_collections(self):
        return ["eagle"]

    def delete_collection(self, **_kwargs):
        self.rows.clear()

    def collection_info(self, **_kwargs):
        return {"count": len(self.rows)}

    def list(self, **_kwargs):
        return list(self.rows.values())

    def healthcheck(self, **_kwargs):
        return True


def test_kylin_vector_store_converts_cosine_distance_to_similarity():
    store = KylinVectorStore("eagle", 2, "vendor-cosine", "cosine_distance", FakeClient())

    results = store.search("query", [1.0, 2.0], top_k=1, filters={"user_id": "u1"})

    assert results[0].id == "v1"
    assert results[0].score == 0.75
    assert store.healthcheck() is True


def test_kylin_vector_store_converts_l2_distance_to_similarity():
    store = KylinVectorStore("eagle", 2, "vendor-l2", "l2_distance", FakeClient())

    results = store.search("query", [1.0, 2.0], top_k=1)

    assert results[0].score == 0.8
