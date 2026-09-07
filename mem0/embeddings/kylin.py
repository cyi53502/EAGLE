from typing import Literal, Optional

from mem0.embeddings.base import EmbeddingBase


class KylinEmbedding(EmbeddingBase):
    def __init__(self, config):
        super().__init__(config)
        self.client = config.client
        self.dimension = config.embedding_dims

    def embed(
        self,
        text: str,
        memory_action: Optional[Literal["add", "search", "update"]] = None,
    ) -> list[float]:
        vector = self.client.embed(text=text, action=memory_action)
        self._validate_dimension(vector)
        return [float(value) for value in vector]

    def embed_batch(self, texts, memory_action="add"):
        vectors = self.client.embed_batch(texts=texts, action=memory_action)
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"Kylin embed_batch returned {len(vectors)} vectors for {len(texts)} texts"
            )
        for vector in vectors:
            self._validate_dimension(vector)
        return [[float(value) for value in vector] for vector in vectors]

    def healthcheck(self):
        return bool(self.client.healthcheck())

    def _validate_dimension(self, vector):
        if len(vector) != self.dimension:
            raise RuntimeError(
                f"Kylin embedding dimension mismatch: expected {self.dimension}, got {len(vector)}"
            )
