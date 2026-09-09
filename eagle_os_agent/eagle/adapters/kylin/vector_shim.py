"""Brute-force KylinVectorClient for stage 8 and governance tests.

No kylin-ai-vector-engine daemon required.  COSINE distance over unit vectors;
every operator in PROBE_FILTER_DIALECT is supported.  Implementation uses the
same normalized payload dialect that capability_smoke.py probes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ShimVectorRow:
    id: str
    payload: dict
    score: float = 0.0
    vector: list[float] | None = None


def _cosine_distance(a: list[float], b: list[float]) -> float:
    return 1.0 - sum(x * y for x, y in zip(a, b))


def _matches(payload, filters) -> bool:
    row_id = payload.get("__id__")
    for key, expected in filters.items():
        if key == "$not":
            if any(_matches(payload, c) for c in expected):
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


class ShimVectorClient:
    """Drops into the KylinVectorClient Protocol shape."""

    def __init__(self):
        self._collections: dict[str, dict[str, ShimVectorRow]] = {}
        self._schemas: dict[str, dict] = {}

    def ensure_collection(self, *, name: str, dimension: int, metric: str) -> None:
        self._collections.setdefault(name, {})
        self._schemas[name] = {"dimension": dimension, "metric": metric}

    def list_collections(self):
        return list(self._collections.keys())

    def collection_info(self, *, name: str):
        return self._schemas.get(name)

    def delete_collection(self, *, name: str) -> None:
        self._collections.pop(name, None)
        self._schemas.pop(name, None)

    def healthcheck(self, *, collection: str) -> bool:
        return collection in self._collections

    def upsert(self, *, collection: str, vectors, payloads, ids) -> None:
        col = self._collections[collection]
        for vector, payload, vector_id in zip(vectors, payloads, ids):
            col[vector_id] = ShimVectorRow(
                id=vector_id, vector=list(vector), payload=dict(payload) | {"__id__": vector_id}
            )

    def get(self, *, collection: str, vector_id: str, include_vector: bool):
        row = self._collections.get(collection, {}).get(vector_id)
        return row if row is not None else None

    def search(self, *, collection: str, vector, limit: int, filters: dict):
        col = self._collections.get(collection, {})
        if not col:
            return []
        query_vec = vector if not (isinstance(vector, list) and vector and isinstance(vector[0], list)) else vector[0]
        scored: list[tuple[float, ShimVectorRow]] = []
        for row in col.values():
            if not _matches(row.payload, filters or {}):
                continue
            scored.append((_cosine_distance(row.vector, query_vec), row))
        scored.sort(key=lambda t: t[0])
        return [
            ShimVectorRow(id=row.id, vector=row.vector, payload=row.payload, score=dist) for dist, row in scored[:limit]
        ]

    def list(self, *, collection: str, filters: dict, limit: int | None):
        col = self._collections.get(collection, {})
        rows = [r for r in col.values() if _matches(r.payload, filters or {})]
        if limit is not None:
            rows = rows[:limit]
        return [ShimVectorRow(id=r.id, vector=r.vector, payload=r.payload, score=0.0) for r in rows]

    def delete(self, *, collection: str, vector_id: str) -> None:
        self._collections.get(collection, {}).pop(vector_id, None)
