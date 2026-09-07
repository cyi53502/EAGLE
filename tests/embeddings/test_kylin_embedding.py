import pytest

from mem0.configs.embeddings.kylin import KylinEmbeddingConfig
from mem0.embeddings.kylin import KylinEmbedding


class FakeClient:
    def embed(self, *, text, action):
        return [len(text), 2]

    def embed_batch(self, *, texts, action):
        return [[len(text), 2] for text in texts]

    def healthcheck(self):
        return True


def test_kylin_embedding_uses_injected_client_and_validates_dimension():
    embedder = KylinEmbedding(
        KylinEmbeddingConfig(embedding_dims=2, client=FakeClient())
    )

    assert embedder.embed("abc", "search") == [3.0, 2.0]
    assert embedder.embed_batch(["a", "bb"]) == [[1.0, 2.0], [2.0, 2.0]]
    assert embedder.healthcheck() is True


def test_kylin_embedding_rejects_wrong_dimension():
    embedder = KylinEmbedding(KylinEmbeddingConfig(embedding_dims=3, client=FakeClient()))

    with pytest.raises(RuntimeError, match="dimension mismatch"):
        embedder.embed("abc", "add")
