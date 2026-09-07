from typing import Literal, Protocol

MemoryAction = Literal["add", "search", "update"] | None


class KylinEmbeddingClient(Protocol):
    """Boundary implemented against the concrete Kylin SDK used by deployment."""

    def embed(self, *, text: str, action: MemoryAction) -> list[float]: ...

    def embed_batch(self, *, texts: list[str], action: MemoryAction) -> list[list[float]]: ...

    def healthcheck(self) -> bool: ...
