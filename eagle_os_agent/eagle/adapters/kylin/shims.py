"""
Real-vs-shim Kylin SDK binding note.

The real vector path talks to the live ``kylin-ai-vector-engine`` daemon over
its unix socket through the C ABI bridge ``libkylin_vec_bridge.so`` (see
``kylin_vec_bridge.cpp``).  The installed C++ SDK headers are stale relative
to the shipped ``libkysdk-vector-engine-client.so.1`` ABI, so the bridge
constructs ``SearchArguments``/``QueryArguments`` at reverse-engineered real
offsets instead of relying on the headers' inline constructors.

Selection is env-driven and reported honestly via ``client.backend``:

  KYLIN_USE_SHIM (default "1"):
    "0"           -> try real clients:
                       embedding: RealEmbeddingClient -> backend "onnx" when
                         KYLIN_EMBEDDING_MODEL points at existing gte-base
                         weights, else "shim(fallback: <Exc>)"
                       vector: RealVectorClient (bridge over KYLIN_VECTOR_UDS,
                         default /tmp/kylin-ai-vector-engine-0.sock)
                         -> backend "real", else "shim(fallback: <Exc>)"
    anything else -> deterministic shims, backend "shim"

Factories (the injection sites):
  make_embedding_client(dim)     eagle/adapters/kylin/real_embedding.py
  make_vector_client()           eagle/adapters/kylin/real_vector.py
  create_auto_gateway(...)       eagle/bootstrap.py -> Mem0Gateway whose
                                 .backend == {"embedding": ..., "vector": ...}

Harnesses/reports must surface ``gateway.backend`` so a shim run is never
mislabeled as the real SDK.

Metrics / semantics (verified empirically against the real engine):
  distance_metric = "cosine_distance"
  score_semantics = "cosine_distance"  (engine returns cosine SIMILARITY, best
                    first; we expose row.score = max(0, 1 - sim) = distance)
  embedding_model_dims = 768
"""

from eagle.adapters.kylin.embedding_shim import ShimEmbeddingClient
from eagle.adapters.kylin.vector_shim import ShimVectorClient

__all__ = ["ShimEmbeddingClient", "ShimVectorClient"]
