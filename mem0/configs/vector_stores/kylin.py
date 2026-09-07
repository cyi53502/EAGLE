from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class KylinVectorStoreConfig(BaseModel):
    collection_name: str = Field("eagle_memories", min_length=1)
    embedding_model_dims: int = Field(gt=0)
    distance_metric: str = Field(min_length=1)
    score_semantics: Literal["cosine_distance", "l2_distance", "similarity"]
    client: Any

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")
