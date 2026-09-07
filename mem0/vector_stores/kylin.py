from typing import Any, Dict, Optional

from pydantic import BaseModel

from mem0.vector_stores.base import VectorStoreBase


class OutputData(BaseModel):
    id: str
    score: Optional[float] = None
    payload: Optional[Dict[str, Any]] = None


class KylinVectorStore(VectorStoreBase):
    def __init__(
        self,
        collection_name: str,
        embedding_model_dims: int,
        distance_metric: str,
        score_semantics: str,
        client,
    ):
        self.collection_name = collection_name
        self.embedding_model_dims = embedding_model_dims
        self.distance_metric = distance_metric
        self.score_semantics = score_semantics
        self.client = client
        self.create_col(collection_name, embedding_model_dims, distance_metric)

    def create_col(self, name, vector_size, distance):
        self.client.ensure_collection(name=name, dimension=vector_size, metric=distance)

    def insert(self, vectors, payloads=None, ids=None):
        self.client.upsert(
            collection=self.collection_name,
            vectors=vectors,
            payloads=payloads or [{} for _ in vectors],
            ids=ids,
        )

    def search(self, query, vectors, top_k=5, filters=None):
        rows = self.client.search(
            collection=self.collection_name,
            vector=vectors,
            limit=top_k,
            filters=filters or {},
        )
        return [
            OutputData(
                id=str(row.id),
                score=self._to_similarity(row.score),
                payload=dict(row.payload or {}),
            )
            for row in rows
        ]

    def delete(self, vector_id):
        self.client.delete(collection=self.collection_name, vector_id=vector_id)

    def update(self, vector_id, vector=None, payload=None):
        if vector is None:
            existing = self.client.get(
                collection=self.collection_name,
                vector_id=vector_id,
                include_vector=True,
            )
            if existing is None:
                raise ValueError(f"Vector {vector_id} not found")
            vector = existing.vector

        self.client.upsert(
            collection=self.collection_name,
            vectors=[vector],
            payloads=[payload or {}],
            ids=[vector_id],
        )

    def get(self, vector_id):
        row = self.client.get(
            collection=self.collection_name,
            vector_id=vector_id,
            include_vector=False,
        )
        if row is None:
            return None
        return OutputData(id=str(row.id), payload=dict(row.payload or {}))

    def list_cols(self):
        return self.client.list_collections()

    def delete_col(self):
        self.client.delete_collection(name=self.collection_name)

    def col_info(self):
        return self.client.collection_info(name=self.collection_name)

    def list(self, filters=None, top_k=None):
        rows = self.client.list(
            collection=self.collection_name,
            filters=filters or {},
            limit=top_k,
        )
        return [OutputData(id=str(row.id), payload=dict(row.payload or {})) for row in rows]

    def reset(self):
        self.delete_col()
        self.create_col(self.collection_name, self.embedding_model_dims, self.distance_metric)

    def healthcheck(self):
        return bool(self.client.healthcheck(collection=self.collection_name))

    def _to_similarity(self, value):
        value = float(value)
        if self.score_semantics == "cosine_distance":
            return max(0.0, 1.0 - value)
        if self.score_semantics == "l2_distance":
            return 1.0 / (1.0 + value)
        return value
