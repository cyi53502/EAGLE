import os
import sys

from eagle.adapters.mem0_gateway import Mem0Gateway

os.environ["MEM0_TELEMETRY"] = "False"


def create_mem0_gateway(
    *,
    embedding_client,
    vector_client,
    embedding_dims: int,
    distance_metric: str,
    score_semantics: str,
    history_db_path: str,
    collection_name: str = "eagle_memories",
):
    telemetry_module = sys.modules.get("mem0.memory.telemetry")
    if telemetry_module is not None and telemetry_module.MEM0_TELEMETRY:
        raise RuntimeError("Mem0 telemetry was enabled before EAGLE bootstrap")

    from mem0 import Memory

    memory = Memory.from_config(
        {
            "embedder": {
                "provider": "kylin",
                "config": {
                    "embedding_dims": embedding_dims,
                    "client": embedding_client,
                },
            },
            "vector_store": {
                "provider": "kylin",
                "config": {
                    "collection_name": collection_name,
                    "embedding_model_dims": embedding_dims,
                    "distance_metric": distance_metric,
                    "score_semantics": score_semantics,
                    "client": vector_client,
                },
            },
            "llm": {"provider": "noop", "config": {}},
            "history_db_path": history_db_path,
        }
    )
    return Mem0Gateway(memory)


def create_auto_gateway(
    *,
    history_db_path: str,
    collection_name: str = "eagle_memories",
    embedding_dims: int = 768,
    distance_metric: str = "cosine_distance",
    score_semantics: str = "cosine_distance",
):
    """Env-driven gateway: pick shim vs real Kylin clients automatically.

    ``KYLIN_USE_SHIM`` (default "1"):
      "0"          -> try real clients (ONNX embedding when
                      KYLIN_EMBEDDING_MODEL points at existing weights; real
                      vector engine over KYLIN_VECTOR_UDS, default
                      /tmp/kylin-ai-vector-engine-0.sock).  On failure the
                      client falls back to the shim but tags ``backend``
                      "shim(fallback: <Exc>)" so reports stay honest.
      anything else -> deterministic shims.

    The returned ``Mem0Gateway`` runs the capability probe in its
    constructor (fail-fast on missing required capabilities) and exposes
    ``.backend`` = {"embedding": ..., "vector": ...} for honest reporting.
    """
    from eagle.adapters.kylin.real_embedding import make_embedding_client
    from eagle.adapters.kylin.real_vector import make_vector_client

    embedding_client = make_embedding_client(embedding_dims)
    vector_client = make_vector_client()
    return create_mem0_gateway(
        embedding_client=embedding_client,
        vector_client=vector_client,
        embedding_dims=embedding_dims,
        distance_metric=distance_metric,
        score_semantics=score_semantics,
        history_db_path=history_db_path,
        collection_name=collection_name,
    )
