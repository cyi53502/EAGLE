from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class KylinEmbeddingConfig(BaseModel):
    model: Optional[str] = Field(default=None, min_length=1, description="Kylin embedding model name")
    embedding_dims: int = Field(description="Embedding vector dimension", gt=0)
    client: Any = Field(description="Configured Kylin embedding client adapter")

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")
