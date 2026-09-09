"""
Stage-8 SDK binding note — how to swap shims for the real SDKs.

Embedding (real):
    from kylin_ai import TextEmbeddingSession  # libkysdk-embedding C++ wrapper
    client = KylinEmbeddingClient(session=TextEmbeddingSession(...),
                                  model_name="ensemble-embd_gte-base_uint8-text")
    # or: from openkylin.kylin_embedding import KylinEmbedding
In gate construction (eagle/bootstrap.py):
    create_mem0_gateway(embedding_client=real_embedding_client, ...)

Vector (real):
    from kysdk_vector_engine_client import Database
    db = Database.Create(); db.Connect(ConnectParam("eagle"))
    vector_client = KylinVectorClient(db=db)
    # or: wrap via eagle.adapters.kylin.vector_client adapter that delegates to Database

Both shims implement the two Kylin*Client Protocols verbatim, so the swap is
a single injection-site change.

Metrics / semantics confirmed by this stage:
  distance_metric = "cosine_distance"
  score_semantics = "cosine_distance"   (similarity = max(0, 1 - distance))
  embedding_model_dims = 768
"""

from eagle.adapters.kylin.embedding_shim import ShimEmbeddingClient
from eagle.adapters.kylin.vector_shim import ShimVectorClient

__all__ = ["ShimEmbeddingClient", "ShimVectorClient"]
