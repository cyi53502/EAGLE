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
