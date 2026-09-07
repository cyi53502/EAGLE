from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class KylinVectorRow:
    id: str
    score: float
    payload: dict
    vector: list[float] | None = None


class KylinVectorClient(Protocol):
    """Normalized boundary; SDK-specific request/response conversion belongs behind it.

    Filter dialect (probed at startup by
    ``eagle.adapters.kylin.capabilities.probe_vector_capabilities``):
    equality ``{"field": value}``, membership ``{"field": {"in": [...]}}``,
    exclusion ``{"field": {"nin": [...]}}``, inequality ``{"field": {"ne": value}}``,
    vector-ID allowlist/blocklist ``{"id": {"in"/"nin": [...]}}``,
    negation ``{"$not": [condition, ...]}``.
    """

    def ensure_collection(self, *, name: str, dimension: int, metric: str) -> None: ...

    def upsert(self, *, collection: str, vectors, payloads, ids) -> None: ...

    def search(self, *, collection: str, vector, limit: int, filters: dict) -> list[KylinVectorRow]: ...

    def delete(self, *, collection: str, vector_id: str) -> None: ...

    def get(self, *, collection: str, vector_id: str, include_vector: bool) -> KylinVectorRow | None: ...

    def list_collections(self): ...

    def delete_collection(self, *, name: str) -> None: ...

    def collection_info(self, *, name: str): ...

    def list(self, *, collection: str, filters: dict, limit: int | None) -> list[KylinVectorRow]: ...

    def healthcheck(self, *, collection: str) -> bool: ...
