# EAGLE 基于 Mem0 OSS 的代码修改实现方案

> 目标：以 Mem0 OSS Python 为底层代码基座，在不侵入 `mem0/memory/main.py` 的前提下，实现 EAGLE 治理层，并用麒麟 SDK 替换默认 Embedding 与 VectorStore。  
> Mem0 基线 commit：`9a7924befd7026e41e445ba809370009e5e985a6`  
> Mem0 Python 包版本：`2.0.20`  
> 本文只描述代码实施，不重复理论论证。

---

## 1. 最终工程边界

```text
Mem0 OSS Python
= Memory API、Embedding Provider API、VectorStore Provider API

EAGLE
= Episode、Evidence、Candidate、Gate、Preference、Knowledge、PACK、冲突、遗忘

EAGLE SQLite
= 权威状态

Mem0 + Kylin Vector
= 派生语义索引
```

生产写入的唯一合法路径：

```text
Episode
→ Attribution
→ Candidate
→ Commitment Gate
→ COMMITTED
→ EAGLE SQLite + EvidenceLink + IndexJob
→ Outbox Worker
→ Memory.add(..., infer=False)
```

禁止出现：

```python
# mem0/memory/main.py
if eagle:
    ...
```

也禁止 EAGLE 正式写入路径调用：

```python
memory.add(..., infer=True)
```

---

## 2. 成功标准

代码实施完成必须同时满足：

1. `mem0/memory/main.py` 无 EAGLE 分支、无业务状态判断。
2. `Memory.from_config` 能构造 `kylin + kylin + noop` 三个 Provider。
3. `Memory.add(..., infer=False)` 能通过麒麟 embedding 写入麒麟 VectorStore。
4. Fallback 成功只产生 Knowledge Candidate，不产生 Preference Candidate。
5. `PENDING` Candidate 不进入正式 Memory 表，也不产生 UPSERT IndexJob。
6. COMMITTED Memory 必须关联 Evidence。
7. HARD Preference 在 Planner 前物理过滤工具。
8. PACK 的 Knowledge 结果必须与 SQLite 当前 `ACTIVE` 状态取交集。
9. `FORGETTING` 状态提交后，即使向量尚未删除，PACK 也不能返回该 Memory。
10. 删除 Mem0/麒麟索引后，可以从 EAGLE SQLite 重建所有 ACTIVE 派生索引。
11. 配置错误、维度错误、SDK 错误不能触发默认 Provider fallback。
12. user tenant filter capability 未通过时，服务启动失败。

### 2.1 当前实现状态（2026-09-06）

已落地：

- Mem0 `kylin` Embedding、`kylin` VectorStore、`noop` LLM 的配置和 Factory 注册；
- `Memory.from_config(kylin/kylin/noop)` 的 raw add、search、delete 集成测试；
- Episode、Evidence、Candidate、规则归因与 Commitment Gate；
- Preference/Knowledge 权威表、证据链接、不可变 Preference 版本链；
- Knowledge Outbox UPSERT/DELETE、幂等 `index_key`、启动 reconciliation（含超租约 RUNNING 复位、duplicate 清理调度）；
- SQLite Preference Resolver（`created_at` 平局裁决 + unresolved conflict 抛出）、HARD Constraint Compiler（含 `NO_FEASIBLE_ACTION`）、PACK 权威态后过滤；
- P-K Visibility、K-K 冲突标记（矛盾证据达到 Knowledge 门槛后才替换旧版本）、环境作用域隔离与 revalidation request、Preference/Knowledge forgetting；
- OS Tool Executor 统一 Episode 采集；
- 最小 HTTP API 与 provider/capability 健康信息（`/health` 全字段，`/capabilities` 含启动实测能力矩阵）；
- Filter Capability Probe 实测 EQ/IN/NE/NOT/AND/ID allowlist/ID blocklist/list+filter/read-after-write/delete（见 §35），失败能力记录于 `/capabilities` 的 `capability_errors`。

未落地（声明过的待办）：

- 主动 Revalidation 工具执行器（当前已有 request、环境隔离以及新执行证据形成版本的闭环）；
- Alembic 迁移（`create_schema` 为原型初始化路径）；
- 真实麒麟 SDK 接入（当前注入归一化 fake/归一化 client 边界）。

真实麒麟 SDK 的包名、请求类型和响应类型尚未提供。因此当前实现把已归一化的 SDK client 作为严格依赖注入，接口定义位于 `eagle_os_agent/eagle/adapters/kylin/`。没有编造 SDK 方法，也没有加入默认 Provider fallback。接入真实 SDK 时只需实现这两个边界并完成真实引擎测试。

当前 SQLite 初始化使用 SQLAlchemy `create_all`，尚未引入 Alembic；这是原型启动方式，不是生产迁移方案。引入 Alembic 前需要先确定应用部署和发布流程，避免同时维护两套迁移入口。

---

## 3. 推荐目录结构

```text
/root/rivermind-data/mem0/
├── mem0/
│   ├── memory/main.py                         # 不修改
│   ├── embeddings/
│   │   └── kylin.py                          # 新增
│   ├── vector_stores/
│   │   └── kylin.py                          # 新增
│   ├── llms/
│   │   └── noop.py                           # 新增
│   ├── configs/
│   │   ├── embeddings/
│   │   │   └── kylin.py                      # 新增
│   │   └── vector_stores/
│   │       └── kylin.py                      # 新增
│   ├── embeddings/configs.py                 # 修改白名单
│   ├── vector_stores/configs.py              # 修改配置映射
│   ├── llms/configs.py                       # 修改白名单
│   └── utils/factory.py                      # 注册 Provider
│
├── tests/
│   ├── embeddings/test_kylin_embedding.py    # 新增
│   ├── vector_stores/test_kylin_vector_store.py # 新增
│   ├── llms/test_noop.py                     # 新增
│   └── memory/test_kylin_raw_memory.py       # 新增
│
├── eagle_os_agent/                           # 独立应用子项目（wheel 只发布单一顶层包 eagle）
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── migrations/
│   ├── eagle/
│   │   ├── domain/
│   │   ├── db/
│   │   ├── episode/
│   │   ├── attribution/
│   │   ├── candidate/
│   │   ├── gate/
│   │   ├── preference/
│   │   ├── knowledge/
│   │   ├── pack/
│   │   ├── conflict/
│   │   ├── forgetting/
│   │   ├── outbox/
│   │   ├── adapters/
│   │   │   ├── mem0_gateway.py
│   │   │   ├── kylin/
│   │   │   │   ├── capabilities.py
│   │   │   │   ├── embedding_client.py
│   │   │   │   └── vector_client.py
│   │   │   └── os_tools/executor.py
│   │   └── api/
│   └── tests/
│       ├── unit/
│       ├── integration/
│       ├── scenarios/
│       └── benchmark/
│
└── EAGLE_MEM0_CODE_IMPLEMENTATION_PLAN.md
```

EAGLE 使用独立子项目，是因为当前根 `pyproject.toml` 的 wheel 只包含 `mem0`。直接在根目录新增 `eagle/`，安装 wheel 后不会包含该包。

### 3.1 当前源码依据

| 实施判断 | 源码 |
|---|---|
| `Memory` 初始化时构造三个 Provider | [`mem0/memory/main.py`](mem0/memory/main.py#L487-L501) |
| `infer=False` 直接 embedding + insert | [`mem0/memory/main.py`](mem0/memory/main.py#L879-L914) |
| `search` 使用 `filters` 传 entity scope | [`mem0/memory/main.py`](mem0/memory/main.py#L1379-L1461) |
| 搜索会过采样并尝试 keyword/entity 信号 | [`mem0/memory/main.py`](mem0/memory/main.py#L1628-L1731) |
| raw insert 在内部生成 UUID | [`mem0/memory/main.py`](mem0/memory/main.py#L1961-L1991) |
| VectorStore 完整抽象 | [`mem0/vector_stores/base.py`](mem0/vector_stores/base.py#L4-L100) |
| 三类 Provider 的 Factory | [`mem0/utils/factory.py`](mem0/utils/factory.py#L35-L223) |
| 根 wheel 只包含 `mem0` | [`pyproject.toml`](pyproject.toml#L92-L105) |

该方案绑定上述 commit。升级 Mem0 后，先重新核验这些位置，再迁移修改。

---

# 第一部分：Mem0 Provider 层修改

## 4. 修改总表

| 文件 | 类型 | 目的 |
|---|---|---|
| `mem0/configs/embeddings/kylin.py` | 新增 | 麒麟 Embedding 配置 |
| `mem0/embeddings/kylin.py` | 新增 | 实现 `EmbeddingBase` |
| `mem0/embeddings/configs.py` | 修改 | 允许 `provider="kylin"` |
| `mem0/configs/vector_stores/kylin.py` | 新增 | 麒麟 VectorStore 配置 |
| `mem0/vector_stores/kylin.py` | 新增 | 实现 `VectorStoreBase` |
| `mem0/vector_stores/configs.py` | 修改 | 绑定 Kylin 配置类 |
| `mem0/llms/noop.py` | 新增 | 误调用 LLM 时快速失败 |
| `mem0/llms/configs.py` | 修改 | 允许 `provider="noop"` |
| `mem0/utils/factory.py` | 修改 | 注册三个 Provider |
| `pyproject.toml` | 条件修改 | 麒麟 SDK optional dependency |

当前各分类的 `__init__.py` 是空文件，实际注册发生在配置白名单和 `mem0/utils/factory.py`，不需要为了注册修改 `__init__.py`。

---

## 5. Kylin Embedding 配置

新增：

```text
mem0/configs/embeddings/kylin.py
```

建议结构：

```python
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class KylinEmbeddingConfig(BaseModel):
    model: Optional[str] = Field(default=None, description="Kylin embedding model name")
    embedding_dims: int = Field(gt=0)
    client: Any

    model_config = ConfigDict(arbitrary_types_allowed=True)
```

这里只校验系统边界：模型名、维度和已构造的 SDK adapter。真实 SDK 若使用 endpoint、tenant、token 等原始参数，应由 `adapters/kylin/embedding_client.py` 负责从安全配置构造 client，不把密钥处理散落到 Mem0 Provider。

如果最终 SDK 不适合注入 client，可将配置改为原始连接参数，并在 `KylinEmbedding` 中构造官方 SDK。两种方式必须二选一，不同时保留两套配置路径。

由于 Mem0 会为 telemetry/entity store 复制 VectorStore 配置，注入对象方案必须测试 `deepcopy` 与多 collection 行为。若官方 client 不可复制，配置中改为可复制的 `client_factory`，由每个 Provider 实例取 client；不要同时保留 `client` 和 `client_factory` 两条生产路径。

---

## 6. KylinEmbedding

新增：

```text
mem0/embeddings/kylin.py
```

实现：

```python
from typing import Literal

from mem0.embeddings.base import EmbeddingBase


class KylinEmbedding(EmbeddingBase):
    def __init__(self, config):
        super().__init__(config)
        self.client = config.client
        self.dimension = config.embedding_dims

    def embed(
        self,
        text: str,
        memory_action: Literal["add", "search", "update"] | None = None,
    ) -> list[float]:
        vector = self.client.embed(text=text, action=memory_action)

        if len(vector) != self.dimension:
            raise RuntimeError(
                f"Kylin embedding dimension mismatch: "
                f"expected {self.dimension}, got {len(vector)}"
            )

        return [float(value) for value in vector]

    def embed_batch(self, texts, memory_action="add"):
        vectors = self.client.embed_batch(texts=texts, action=memory_action)

        if len(vectors) != len(texts):
            raise RuntimeError(
                f"Kylin embed_batch returned {len(vectors)} vectors "
                f"for {len(texts)} texts"
            )

        for vector in vectors:
            if len(vector) != self.dimension:
                raise RuntimeError(
                    f"Kylin embedding dimension mismatch: "
                    f"expected {self.dimension}, got {len(vector)}"
                )

        return [[float(value) for value in vector] for vector in vectors]
```

`client.embed` 和 `client.embed_batch` 是 EAGLE adapter 协议名称，不是对麒麟 SDK 的假设。真实 SDK 的方法名和返回值只在 adapter 内转换。

不要添加：

- 空向量 fallback；
- 默认 OpenAI fallback；
- 维度自动截断或补零；
- 宽泛 `except Exception: return []`。

当前 `AsyncMemory` 通过 `asyncio.to_thread` 调用同步 Embedder，因此 client 必须提供同步、线程安全的接口。

---

## 7. 注册 KylinEmbedding

修改 `mem0/embeddings/configs.py`：

```python
if provider in [
    # existing providers
    "kylin",
]:
    return v
```

修改 `mem0/utils/factory.py`。

当前 Factory 对所有 Embedder 固定创建 `BaseEmbedderConfig`，无法接收 `client` 等麒麟专用字段。采用最小扩展：

```python
from mem0.configs.embeddings.kylin import KylinEmbeddingConfig


class EmbedderFactory:
    provider_to_class = {
        # existing mappings unchanged
        "kylin": "mem0.embeddings.kylin.KylinEmbedding",
    }

    provider_to_config = {
        "kylin": KylinEmbeddingConfig,
    }

    @classmethod
    def create(cls, provider_name, config, vector_config):
        if provider_name == "upstash_vector" and vector_config and vector_config.enable_embeddings:
            return MockEmbeddings()

        class_path = cls.provider_to_class.get(provider_name)
        if class_path is None:
            raise ValueError(f"Unsupported Embedder provider: {provider_name}")

        config_class = cls.provider_to_config.get(provider_name, BaseEmbedderConfig)
        typed_config = config_class(**config) if isinstance(config, dict) else config
        return load_class(class_path)(typed_config)
```

现有 provider 仍使用 `BaseEmbedderConfig`，不做批量迁移。

---

## 8. Kylin VectorStore 配置

新增：

```text
mem0/configs/vector_stores/kylin.py
```

建议结构：

```python
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class KylinVectorStoreConfig(BaseModel):
    collection_name: str = Field(default="eagle_memories", min_length=1)
    embedding_model_dims: int = Field(gt=0)
    distance_metric: Literal[
        "cosine_distance",
        "l2_distance",
        "inner_product",
    ]
    client: Any

    @model_validator(mode="before")
    @classmethod
    def reject_extra_fields(cls, values):
        extra = set(values) - set(cls.model_fields)
        if extra:
            raise ValueError(f"Extra Kylin vector fields: {sorted(extra)}")
        return values

    model_config = ConfigDict(arbitrary_types_allowed=True)
```

`distance_metric` 的最终枚举必须按真实麒麟 SDK 确认。未确认之前，不应把示例枚举提交为生产实现。

若使用注入 client，该对象还必须支持 Mem0 为 entity/telemetry 派生配置时的复制行为。生产默认关闭 telemetry 不能替代 entity collection 集成测试。

修改 `mem0/vector_stores/configs.py`：

```python
_provider_configs = {
    # existing mappings unchanged
    "kylin": "KylinVectorStoreConfig",
}
```

当前代码会自动导入：

```text
mem0.configs.vector_stores.kylin.KylinVectorStoreConfig
```

---

## 9. KylinVectorStore

新增：

```text
mem0/vector_stores/kylin.py
```

返回对象：

```python
from typing import Any

from pydantic import BaseModel


class OutputData(BaseModel):
    id: str
    score: float | None = None
    payload: dict[str, Any] | None = None
```

Provider 骨架：

```python
from mem0.vector_stores.base import VectorStoreBase


class KylinVectorStore(VectorStoreBase):
    def __init__(
        self,
        collection_name: str,
        embedding_model_dims: int,
        distance_metric: str,
        client,
    ):
        self.collection_name = collection_name
        self.embedding_model_dims = embedding_model_dims
        self.distance_metric = distance_metric
        self.client = client
        self.create_col(
            name=collection_name,
            vector_size=embedding_model_dims,
            distance=distance_metric,
        )

    def create_col(self, name, vector_size, distance):
        self.client.ensure_collection(
            name=name,
            dimension=vector_size,
            metric=distance,
        )

    def insert(self, vectors, payloads=None, ids=None):
        self.client.upsert(
            collection=self.collection_name,
            vectors=vectors,
            payloads=payloads or [{} for _ in vectors],
            ids=ids,
        )

    def search(self, query, vectors, top_k=5, filters=None):
        rows = self.client.search(
            collection=self.collection_name,
            vector=vectors,
            limit=top_k,
            filters=filters or {},
        )
        return [
            OutputData(
                id=str(row.id),
                score=self._to_similarity(row.score),
                payload=dict(row.payload or {}),
            )
            for row in rows
        ]

    def delete(self, vector_id):
        self.client.delete(self.collection_name, vector_id)

    def update(self, vector_id, vector=None, payload=None):
        if vector is None:
            existing = self.client.get(
                collection=self.collection_name,
                vector_id=vector_id,
                include_vector=True,
            )
            if existing is None:
                raise ValueError(f"Vector {vector_id} not found")
            vector = existing.vector

        self.client.upsert(
            collection=self.collection_name,
            vectors=[vector],
            payloads=[payload or {}],
            ids=[vector_id],
        )

    def get(self, vector_id):
        row = self.client.get(
            collection=self.collection_name,
            vector_id=vector_id,
            include_vector=False,
        )
        if row is None:
            return None
        return OutputData(
            id=str(row.id),
            payload=dict(row.payload or {}),
        )

    def list_cols(self):
        return self.client.list_collections()

    def delete_col(self):
        self.client.delete_collection(self.collection_name)

    def col_info(self):
        return self.client.collection_info(self.collection_name)

    def list(self, filters=None, top_k=None):
        rows = self.client.list(
            collection=self.collection_name,
            filters=filters or {},
            limit=top_k,
        )
        return [
            OutputData(
                id=str(row.id),
                payload=dict(row.payload or {}),
            )
            for row in rows
        ]

    def reset(self):
        self.delete_col()
        self.create_col(
            self.collection_name,
            self.embedding_model_dims,
            self.distance_metric,
        )

    def _to_similarity(self, value):
        if self.distance_metric == "cosine_distance":
            return max(0.0, 1.0 - value)
        if self.distance_metric == "l2_distance":
            return 1.0 / (1.0 + value)
        return value
```

这里的 client 方法仍是 EAGLE adapter 协议，不是麒麟 SDK 的实际名称。

必须满足以下行为：

- `search` 返回 score 越大越相似；
- `get` 找不到时返回 `None`；
- `list` 返回 flat `list[OutputData]`；
- `update(vector=None)` 保留旧 vector；
- `insert` 支持 Mem0 传入的 UUID；
- payload 完整保存 `user_id`、`memory_kind`、`eagle_memory_id` 和 `index_key`；
- 不支持的 filter operator 明确抛错；
- SDK 错误向上抛出。

如果麒麟 `get` 无法返回原 vector，且不支持 metadata-only update，则必须先解决该能力缺口，不能把 `vector=None` 当成空向量写入。

---

## 10. 注册 KylinVectorStore

修改 `mem0/utils/factory.py`：

```python
class VectorStoreFactory:
    provider_to_class = {
        # existing mappings unchanged
        "kylin": "mem0.vector_stores.kylin.KylinVectorStore",
    }
```

当前 `VectorStoreFactory.create` 会对 Pydantic 配置执行 `model_dump()`，然后调用：

```python
KylinVectorStore(**config)
```

因此 config 字段名必须与构造函数参数严格一致。

---

## 11. NoopLLM

新增：

```text
mem0/llms/noop.py
```

```python
from mem0.llms.base import LLMBase


class NoopLLM(LLMBase):
    def generate_response(
        self,
        messages,
        tools=None,
        tool_choice="auto",
        **kwargs,
    ):
        raise RuntimeError(
            "NoopLLM was invoked. "
            "EAGLE persistence must call Memory.add with infer=False."
        )
```

修改 `mem0/llms/configs.py`，加入：

```python
"noop"
```

修改 `mem0/utils/factory.py`：

```python
class LlmFactory:
    provider_to_class = {
        # existing mappings unchanged
        "noop": (
            "mem0.llms.noop.NoopLLM",
            BaseLlmConfig,
        ),
    }
```

运行配置必须传：

```python
"llm": {
    "provider": "noop",
    "config": {"model": "noop"},
}
```

NoopLLM 必须抛错，不能返回空结果。返回空字符串会把 `infer=True` 误用伪装为“没有抽取到事实”。

---

## 12. 依赖与打包

如果麒麟 SDK 是可安装 Python 包，在根 `pyproject.toml` 增加独立 optional group：

```toml
[project.optional-dependencies]
kylin = [
  "<confirmed-kylin-package>==<confirmed-version>",
]
```

不要加入 core `dependencies`，避免所有 Mem0 用户被迫安装麒麟 SDK。

如果 SDK 是本地 wheel、动态库或系统预装组件：

- 不在 `pyproject.toml` 编造包名；
- 在部署镜像或安装脚本中锁定工件校验和；
- adapter 启动时检查 SDK 版本；
- `/capabilities` 返回真实版本。

---

## 13. Mem0 正式配置与启动断言

```python
from mem0 import Memory


config = {
    "embedder": {
        "provider": "kylin",
        "config": {
            "model": settings.kylin_embedding_model,
            "embedding_dims": settings.embedding_dims,
            "client": kylin_embedding_client,
        },
    },
    "vector_store": {
        "provider": "kylin",
        "config": {
            "collection_name": "eagle_memories",
            "embedding_model_dims": settings.embedding_dims,
            "distance_metric": settings.distance_metric,
            "client": kylin_vector_client,
        },
    },
    "llm": {
        "provider": "noop",
        "config": {"model": "noop"},
    },
    "history_db_path": settings.mem0_history_db_path,
}

memory = Memory.from_config(config)

assert memory.config.embedder.provider == "kylin"
assert memory.config.vector_store.provider == "kylin"
assert memory.config.llm.provider == "noop"
assert memory.embedding_model.config.embedding_dims == settings.embedding_dims
assert memory.vector_store.embedding_model_dims == settings.embedding_dims
```

`MEM0_TELEMETRY=False` 必须在导入 `mem0.memory.telemetry` 之前通过进程环境设置，因为当前 Mem0 在模块 import 时读取该变量。

保留 telemetry 时，KylinVectorStore 还会被用于 `mem0migrations` collection。搜索抽取到实体时，Mem0 可能懒加载 `eagle_memories_entities`。Provider 必须支持这些派生 collection，或在实际运行环境中证明相关功能不会启用。

---

# 第二部分：EAGLE 治理层实现

## 14. EAGLE 子项目依赖

`eagle_os_agent/pyproject.toml` 最小依赖：

```toml
[project]
name = "eagle-os-agent"
requires-python = ">=3.10,<4.0"
dependencies = [
  "sqlalchemy>=2.0",
  "alembic>=1.13",
  "pydantic>=2.7",
  "fastapi>=0.115",
  "uvicorn>=0.30",
]
```

Mem0 fork 不应意外从 PyPI 解析为同版本上游包。构建流程应先从当前 commit 构建本地 `mem0ai` wheel，再让 EAGLE 锁定该工件或 VCS commit。

---

## 15. Domain 类型

新增：

```text
eagle_os_agent/eagle/domain/enums.py
eagle_os_agent/eagle/domain/scene.py
eagle_os_agent/eagle/domain/episode.py
eagle_os_agent/eagle/domain/candidate.py
eagle_os_agent/eagle/domain/preference.py
eagle_os_agent/eagle/domain/knowledge.py
```

枚举：

```python
from dataclasses import asdict, dataclass
from enum import Enum


class CandidateType(str, Enum):
    PREFERENCE = "P"
    KNOWLEDGE = "K"


class CandidateState(str, Enum):
    PENDING = "PENDING"
    COMMITTED = "COMMITTED"
    REJECTED = "REJECTED"
    DEFER = "DEFER"


class PreferenceHardness(str, Enum):
    HARD = "HARD"
    SOFT = "SOFT"


class PreferenceStatus(str, Enum):
    ACTIVE = "ACTIVE"
    MASKED = "MASKED"
    REVOKED = "REVOKED"
    FORGETTING = "FORGETTING"
    FORGOTTEN = "FORGOTTEN"


class KnowledgeStatus(str, Enum):
    ACTIVE = "ACTIVE"
    NEEDS_REVALIDATION = "NEEDS_REVALIDATION"
    REVOKED = "REVOKED"
    FORGETTING = "FORGETTING"
    FORGOTTEN = "FORGOTTEN"
```

Scene：

```python
@dataclass(frozen=True)
class Scene:
    app: str | None = None
    task: str | None = None
    artifact_type: str | None = None

    def normalized(self) -> dict[str, str]:
        return {
            key: value.strip().lower()
            for key, value in asdict(self).items()
            if value is not None
        }

    def specificity(self) -> int:
        return len(self.normalized())
```

只定义当前使用的三个 Scene 维度。出现真实需求后再加字段。

---

## 16. SQLAlchemy ORM 与迁移

新增：

```text
eagle_os_agent/eagle/db/engine.py
eagle_os_agent/eagle/db/orm.py
eagle_os_agent/eagle/db/repositories.py
eagle_os_agent/migrations/
```

### 16.1 表

第一版创建：

```text
episodes
candidates
evidence
preferences
knowledge
memory_evidence_links
pk_visibility
index_jobs
```

### 16.2 核心字段

`episodes`：

```text
id
user_id
session_id
scene_json
request_text
tool_name
arguments_digest
success
result_class
error_code
latency_ms
retry_count
fallback_from
previous_error_code
user_intervention
user_correction_json
environment_fingerprint
created_at
```

`candidates`：

```text
id
candidate_identity
user_id
candidate_type                 P | K
candidate_key
candidate_value_json
scene_json
positive_evidence
negative_evidence
independent_choices
distinct_sessions
same_condition_success
confidence
explicit_user_statement
user_confirmed
has_unresolved_conflict
state                          PENDING | COMMITTED | REJECTED | DEFER
created_at
updated_at
```

`evidence`：

```text
id
episode_id
candidate_id
evidence_type
direction                      POSITIVE | NEGATIVE | NEUTRAL
strength                       STRONG | WEAK
contribution
confidence_delta
condition_json
attribution_reason
created_at
```

`preferences`：

```text
id
lineage_id
user_id
preference_key
preference_value_json
hardness                       HARD | SOFT
scene_json
confidence
version
parent_version_id
status                         ACTIVE | MASKED | REVOKED | FORGETTING | FORGOTTEN
authorization_state
mem0_id
created_at
updated_at
```

`knowledge`：

```text
id
lineage_id
user_id
knowledge_type                 WORKFLOW | CASE | TEMPLATE | FACT
retrieval_text
content_json
scene_json
environment_fingerprint
confidence
version
parent_version_id
status                         ACTIVE | NEEDS_REVALIDATION | REVOKED | FORGETTING | FORGOTTEN
mem0_id
created_at
updated_at
```

`memory_evidence_links`：

```text
id
memory_kind                    P | K
memory_id
evidence_id
created_at
```

`pk_visibility`：

```text
id
preference_version_id
knowledge_version_id
reason
active
created_at
updated_at
```

`index_jobs`：

```text
id
index_key
memory_kind                    P | K
memory_id
operation                      UPSERT | DELETE | DELETE_DUPLICATE
state                          PENDING | RUNNING | DONE | FAILED
target_mem0_id
retry_count
last_error
locked_at
available_at
created_at
updated_at
```

ID 使用 UUID 字符串，时间统一保存 UTC。JSON 字段通过 SQLAlchemy `JSON` 类型声明；枚举列使用明确的数据库约束，避免写入未知状态。

### 16.3 必需唯一约束

```text
candidates(candidate_identity) UNIQUE
evidence(candidate_id, episode_id, evidence_type, direction, attribution_reason) UNIQUE
memory_evidence_links(memory_kind, memory_id, evidence_id) UNIQUE
pk_visibility(preference_version_id, knowledge_version_id) UNIQUE
index_jobs(index_key) UNIQUE
preferences(lineage_id, version) UNIQUE
knowledge(lineage_id, version) UNIQUE
```

`attribution_reason` 必须参与 Evidence 重放身份。同一 Episode 可以同时产生方向相同、但语义不同的显式强证据和隐式弱证据；四列唯一键会错误吞掉其中一条。

`preferences` 和 `knowledge` 增加稳定的 `lineage_id`。同一逻辑 Memory 的各版本共享 lineage，通过 `(lineage_id, version)` 保证版本号唯一；每个版本仍有独立主键 `id`，并通过 `parent_version_id` 指向前一版本。

查询索引：

```text
episodes(user_id, session_id, created_at)
evidence(candidate_id, created_at)
preferences(user_id, status)
knowledge(user_id, status)
pk_visibility(preference_version_id, active)
pk_visibility(knowledge_version_id, active)
index_jobs(state, available_at)
```

### 16.4 `index_jobs` 增加租约字段

```text
state           PENDING | RUNNING | DONE | FAILED
locked_at       nullable datetime
available_at    datetime
retry_count     integer
last_error      nullable text
target_mem0_id  nullable text
```

没有 `locked_at` 时，Worker 在设置 `RUNNING` 后崩溃会永久遗留 job。Reconciliation 将超出租约时间的 `RUNNING` 恢复为 `PENDING`。

### 16.5 两个 SQLite 文件

```text
eagle.db          # EAGLE 权威状态
mem0_history.db   # Mem0 自身 history/messages
```

不要让 Alembic 管理 Mem0 的 history/messages 表。

---

## 17. Candidate Identity

该逻辑直接放在 `eagle_os_agent/eagle/candidate/service.py`，不为一次调用增加独立工具模块。

```python
def candidate_identity(
    user_id: str,
    candidate_type: str,
    key: str,
    value: dict,
    scene: Scene,
) -> str:
    source = "|".join(
        [
            user_id,
            candidate_type,
            key,
            json.dumps(value, sort_keys=True, separators=(",", ":")),
            json.dumps(scene.normalized(), sort_keys=True, separators=(",", ":")),
        ]
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()
```

数据库唯一索引负责最终并发正确性，hash 不是唯一防线。

---

## 18. Episode Collector

新增：

```text
eagle_os_agent/eagle/episode/collector.py
eagle_os_agent/eagle/adapters/os_tools/executor.py
```

所有工具必须通过 executor：

```python
async def execute_tool(tool, arguments, context):
    started = time.perf_counter()
    success = False
    error_code = None

    try:
        result = await tool.execute(**arguments)
        success = True
        return result
    except ToolError as exc:
        error_code = exc.code
        raise
    finally:
        await collector.collect(
            EpisodeInput(
                user_id=context.user_id,
                session_id=context.session_id,
                request_text=context.request_text,
                scene=context.scene,
                tool_name=tool.name,
                arguments_digest=digest(arguments),
                success=success,
                error_code=error_code,
                latency_ms=int((time.perf_counter() - started) * 1000),
                fallback_from=context.fallback_from,
                previous_error_code=context.previous_error_code,
                environment_fingerprint=context.environment_fingerprint,
            )
        )
```

`arguments_digest` 默认保存稳定摘要，不保存完整敏感参数。

Fallback 的 B-success Episode 必须带：

```text
fallback_from=A
previous_error_code=<A error>
```

---

## 19. Attribution

新增：

```text
eagle_os_agent/eagle/attribution/base.py
eagle_os_agent/eagle/attribution/explicit.py
eagle_os_agent/eagle/attribution/rule_based.py
```

接口：

```python
class Attributor(Protocol):
    def attribute(self, episode: Episode) -> list[AttributionResult]:
        ...


@dataclass(frozen=True)
class AttributionResult:
    candidate_type: CandidateType
    key: str
    value: dict[str, object]
    scene: Scene
    positive_delta: int
    negative_delta: int
    confidence_delta: float
    explicit: bool
    reason: str
```

Fallback 规则：

```python
def attribute_fallback(episode):
    if not (episode.fallback_from and episode.success):
        return None

    return AttributionResult(
        candidate_type=CandidateType.KNOWLEDGE,
        key=(
            f"fallback:{episode.fallback_from}:"
            f"{episode.previous_error_code}:{episode.tool_name}"
        ),
        value={
            "type": "WORKFLOW",
            "when": {
                "tool": episode.fallback_from,
                "error_code": episode.previous_error_code,
                "environment": episode.environment_fingerprint,
            },
            "action": {"fallback_to": episode.tool_name},
        },
        scene=episode.scene,
        positive_delta=1,
        negative_delta=0,
        confidence_delta=0.35,
        explicit=False,
        reason="primary tool failed and fallback succeeded",
    )
```

该函数禁止返回 `CandidateType.PREFERENCE`。

显式 Preference 第一版接收结构化事件：

```python
@dataclass(frozen=True)
class ExplicitPreferenceEvent:
    user_id: str
    key: str
    value: dict[str, object]
    hardness: PreferenceHardness
    scene: Scene
```

原始自然语言规则只负责生成该事件，不能直接写 Preference 表。

---

## 20. Candidate Service

新增：

```text
eagle_os_agent/eagle/candidate/service.py
```

职责：

1. 计算 candidate identity；
2. upsert Candidate；
3. 插入 Evidence；
4. Evidence 插入成功后更新计数；
5. 计算 distinct sessions；
6. 返回最新 Candidate 给 Gate。

事务伪代码：

```python
with session.begin():
    candidate = candidate_repository.get_or_create(identity, result)

    inserted = evidence_repository.insert_if_absent(
        candidate_id=candidate.id,
        episode_id=episode.id,
        attribution=result,
    )

    if inserted:
        candidate_repository.apply_delta(candidate.id, result)

    candidate = candidate_repository.get_for_update(candidate.id)
    decision = gate.decide(candidate)
```

SQLite 第一阶段使用单 Worker 或 `BEGIN IMMEDIATE` 串行化写入；唯一约束处理重放。

---

## 21. Commitment Gate

新增：

```text
eagle_os_agent/eagle/gate/commitment.py
```

```python
class CommitmentGate:
    def decide(self, candidate):
        if candidate.candidate_type is CandidateType.PREFERENCE:
            return self._decide_preference(candidate)
        return self._decide_knowledge(candidate)

    def _decide_preference(self, candidate):
        if candidate.has_unresolved_conflict:
            return CandidateState.DEFER

        if candidate.explicit_user_statement:
            return CandidateState.COMMITTED

        if (
            candidate.independent_choices >= 3
            and candidate.distinct_sessions >= 2
            and candidate.negative_evidence == 0
        ):
            return CandidateState.COMMITTED

        return CandidateState.PENDING

    def _decide_knowledge(self, candidate):
        if candidate.user_confirmed:
            return CandidateState.COMMITTED

        if candidate.same_condition_success >= 2:
            return CandidateState.COMMITTED

        return CandidateState.PENDING
```

第一版不加入加权 score。

---

## 22. Commit Service

新增：

```text
eagle_os_agent/eagle/preference/service.py
eagle_os_agent/eagle/knowledge/service.py
```

Candidate 提交必须在同一个 EAGLE SQLite 事务内完成：

```text
Candidate.state = COMMITTED
→ INSERT Preference/Knowledge
→ INSERT MemoryEvidenceLink
→ INSERT IndexJob(UPSERT)
→ COMMIT
```

写入前检查 Evidence 数量至少为 1。

第一阶段建议只为 Knowledge 创建向量 UPSERT Job。Preference 的权威读取全部来自 SQLite；其语义管理索引在 GUI/自然语言管理需求出现后再启用。

如果当前交付明确要求 Preference 也可语义检索，则 P 与 K 使用同一 Worker，只在 metadata 中区分 `memory_kind`。

---

## 23. Mem0 Gateway

新增：

```text
eagle_os_agent/eagle/adapters/mem0_gateway.py
```

```python
class Mem0Gateway:
    def __init__(self, memory: Memory):
        self.memory = memory

    def index_knowledge(self, knowledge, index_key: str) -> str:
        result = self.memory.add(
            knowledge.retrieval_text,
            user_id=knowledge.user_id,
            metadata={
                "memory_kind": "K",
                "eagle_memory_id": knowledge.id,
                "knowledge_type": knowledge.knowledge_type.value,
                "version": knowledge.version,
                "index_key": index_key,
            },
            infer=False,
        )

        rows = result["results"]
        if len(rows) != 1:
            raise RuntimeError(
                f"Expected one Mem0 result, got {len(rows)}"
            )
        return rows[0]["id"]

    def find_by_index_key(self, user_id: str, index_key: str):
        return self.memory.get_all(
            filters={
                "user_id": user_id,
                "index_key": index_key,
            },
            top_k=100,
        )["results"]

    def search_knowledge(self, query: str, user_id: str, top_k: int):
        return self.memory.search(
            query=query,
            filters={
                "user_id": user_id,
                "memory_kind": "K",
            },
            top_k=top_k,
        )["results"]

    def delete(self, mem0_id: str):
        self.memory.delete(memory_id=mem0_id)
```

不要调用旧式接口：

```python
memory.search(query, user_id=user_id)
```

当前 commit 会拒绝顶层 `user_id`。

不要依赖向量 metadata 中的 `status` 判断当前生命周期；它是派生副本，可能滞后。

---

## 24. Outbox Worker

新增：

```text
eagle_os_agent/eagle/outbox/service.py
eagle_os_agent/eagle/outbox/worker.py
eagle_os_agent/eagle/outbox/reconciliation.py
```

### 24.1 Claim

第一阶段运行单 Worker：

```text
BEGIN IMMEDIATE
SELECT oldest eligible PENDING/FAILED job
UPDATE job SET state=RUNNING, locked_at=now
COMMIT
```

网络调用不持有数据库事务。

### 24.2 UPSERT

```python
def process_upsert(job):
    memory = memory_repository.get(job.memory_kind, job.memory_id)

    existing = mem0_gateway.find_by_index_key(
        user_id=memory.user_id,
        index_key=job.index_key,
    )

    if existing:
        ordered = sorted(
            existing,
            key=lambda row: (row.get("created_at") or "", row["id"]),
        )
        canonical = ordered[0]
        duplicate_ids = [row["id"] for row in ordered[1:]]
        complete_upsert(job, memory, canonical["id"], duplicate_ids)
        return

    mem0_id = mem0_gateway.index_knowledge(memory, job.index_key)
    complete_upsert(job, memory, mem0_id, [])
```

`complete_upsert` 在短事务内：

```text
memory.mem0_id = mem0_id
job.state = DONE
为每个 duplicate_id 创建带 target_mem0_id 的 DELETE job
```

重复向量清理 job 使用独立键：

```text
DELETE_DUPLICATE:{duplicate_mem0_id}
```

普通遗忘 DELETE job 的 `target_mem0_id` 取正式 Memory 当前的 `mem0_id`。这样清理重复向量时不会误删 canonical vector，也不需要伪造第二个 EAGLE Memory。

### 24.3 为什么不是 exactly-once

当前 `Memory.add(infer=False)` 在内部生成随机 UUID，不允许调用方指定 ID。

崩溃窗口：

```text
Mem0 add 成功
→ 进程退出
→ SQLite 尚未回填 mem0_id
→ 重试可能再次 add
```

因此第一阶段语义是：

```text
at-least-once delivery
+ index_key reconciliation
+ PACK authoritative deduplication
```

不宣称 exactly-once。

### 24.4 失败

外部错误：

```text
retry_count += 1
last_error = exact error
state = FAILED
available_at = next retry time
locked_at = NULL
```

错误不能吞掉。重试耗尽后维持 FAILED，并在 `/health` 暴露。

---

## 25. Preference Resolver

新增：

```text
eagle_os_agent/eagle/preference/resolver.py
```

匹配规则：Preference Scene 的所有非空字段都等于当前 Scene。

同 key 排序：

```text
specificity DESC
version DESC
created_at DESC
```

```python
def scene_matches(rule_scene: dict, current_scene: dict) -> bool:
    return all(current_scene.get(key) == value for key, value in rule_scene.items())


def resolve(preferences, current_scene):
    matched = [
        preference
        for preference in preferences
        if scene_matches(preference.scene_json, current_scene)
    ]
    matched.sort(
        key=lambda preference: (
            len(preference.scene_json),
            preference.version,
            preference.created_at,
        ),
        reverse=True,
    )
    return collapse_by_key(matched)
```

同等具体且无法判断优先级的冲突不猜测，输出 unresolved conflict。

---

## 26. HARD Constraint Compiler

新增：

```text
eagle_os_agent/eagle/preference/compiler.py
```

```python
@dataclass(frozen=True)
class PlannerConstraint:
    allowed_tools: frozenset[str] | None
    denied_tools: frozenset[str]
    require_offline: bool
    allowed_formats: frozenset[str] | None
    privacy_rules: tuple[str, ...]
```

第一版只接受已有可执行适配器的 key：

```text
preferred_tool
denied_tool
require_offline
```

`allowed_format`、自由文本 `privacy_rule` 以及其他未知 HARD key 必须使编译失败，直到 Planner action/parameter 边界提供对应的物理执行适配器，不能只把它们放入 Prompt。

工具过滤：

```python
def apply_constraints(all_tools, constraints):
    tools = [
        tool
        for tool in all_tools
        if tool.name not in constraints.denied_tools
    ]

    if constraints.allowed_tools is not None:
        tools = [
            tool
            for tool in tools
            if tool.name in constraints.allowed_tools
        ]

    if constraints.require_offline:
        tools = [tool for tool in tools if not tool.requires_network]

    return tools
```

过滤后为空，返回 `NO_FEASIBLE_ACTION`，不绕过 HARD Preference。

---

## 27. PACK Service

新增：

```text
eagle_os_agent/eagle/pack/service.py
eagle_os_agent/eagle/pack/filters.py
eagle_os_agent/eagle/pack/ranking.py
```

```python
def pack(request, scene, environment, user_id, top_k):
    preferences = preference_repository.list_active(user_id)
    resolved = preference_resolver.resolve(preferences, scene)
    constraints = constraint_compiler.compile(resolved.hard)

    raw_results = mem0_gateway.search_knowledge(
        query=request,
        user_id=user_id,
        top_k=top_k * 3,
    )

    eagle_ids = {
        row["metadata"]["eagle_memory_id"]
        for row in raw_results
        if row.get("metadata", {}).get("eagle_memory_id")
    }

    active = knowledge_repository.list_active_by_ids(
        user_id=user_id,
        ids=eagle_ids,
    )

    visible = visibility_filter.apply(active, resolved)
    valid = environment_filter.apply(visible, environment)
    ranked = soft_preference_ranker.rank(valid, resolved.soft)

    return PlannerContext(
        constraints=constraints,
        knowledge=ranked[:top_k],
    )
```

PACK 必须满足：

- 缺少 `eagle_memory_id` 的向量结果丢弃；
- user 不匹配的结果丢弃；
- SQLite 非 ACTIVE 的结果丢弃；
- 相同 `eagle_memory_id` 的重复向量只保留一份；
- 环境不匹配的 K 不返回；
- P-K mask active 的 K 不返回。

---

## 28. Filter Capability Probe

新增：

```text
eagle_os_agent/eagle/adapters/kylin/capabilities.py
```

启动时对隔离 collection 实测：

```text
EQ
IN
NOT / NE
AND
ID allowlist
ID blocklist
list + filter
read-after-write
read-after-delete
```

最低安全门槛：

```text
user_id EQ = supported and correct
```

未通过时服务启动失败。

检索模式：

```text
Mode A: 原生 metadata pre-filter
Mode B: SQLite eligible ID → metadata IN / vector ID allowlist
Mode C: oversample → SQLite post-filter
```

Mode C 只允许降低召回质量，不能用于容忍 tenant filter 失效。

---

## 29. P-P、K-K、P-K Conflict

新增：

```text
eagle_os_agent/eagle/conflict/service.py
```

### P-P

```text
same user
+ same preference_key
+ same normalized scene
+ different value
→ conflict
```

明确的新声明：创建新版本，旧版本 `REVOKED`。

### K-K

相同 Scene、Environment 和 condition 下结果矛盾：先创建 revalidation request；矛盾证据达到 Knowledge 门槛后，旧 K 才进入 `NEEDS_REVALIDATION` 并创建新版本，不因单次矛盾执行立即下线。

### P-K

创建 `pk_visibility`：

```text
preference_version_id
knowledge_version_id
reason
active=true
```

P 撤销后设 `active=false`，K 可恢复。

---

## 30. Environment Revalidation

新增：

```text
eagle_os_agent/eagle/knowledge/revalidation.py
```

第一版环境 fingerprint 包含：

```text
os
os_version
application_version
tool_version
tool_schema_hash
network_class
```

PACK 检测到不匹配：

```text
旧环境版本保持 ACTIVE
创建 (knowledge_id, target_environment) revalidation request
当前环境不返回该 K
```

Revalidation 真实执行结果：

```text
SUCCESS → 创建新环境版本
FAIL    → 当前环境不可用
UNKNOWN → 继续不返回
```

新环境验证成功后必须创建新版本并设置 `parent_version_id`，不覆盖或全局禁用原环境版本。

---

## 31. Forgetting

新增：

```text
eagle_os_agent/eagle/forgetting/service.py
```

事务一：

```text
BEGIN
ACTIVE → FORGETTING
create IndexJob(DELETE)
deactivate visibility
COMMIT
```

此时 PACK 已不可见。

Worker：

```python
def process_delete(job):
    memory = memory_repository.get(job.memory_kind, job.memory_id)

    target_mem0_id = job.target_mem0_id or memory.mem0_id
    if target_mem0_id is not None:
        mem0_gateway.delete(target_mem0_id)

    with session.begin():
        if target_mem0_id == memory.mem0_id:
            memory.mem0_id = None
            memory.status = "FORGOTTEN"
        job.state = "DONE"
```

进入 `FORGOTTEN` 前还必须清空权威内容、删除该 Memory 的 visibility/evidence link 和孤立 Evidence/Candidate。Episode 只保留 `(user_id, execution_id)` 幂等 tombstone，原内容 fingerprint 改为不可反推内容的终态标记，并清空请求、工具、环境和治理结果中的已遗忘 Memory 引用。

当前 Mem0 `delete` 会先 get；找不到时抛 `ValueError`。只有精确确认目标不存在时才把 not-found 当作已收敛，其他错误继续失败。

---

## 32. Reconciliation

启动和定时检查：

```text
ACTIVE + mem0_id NULL
→ ensure UPSERT job

ACTIVE + mem0_id present + vector missing
→ ensure UPSERT job

FORGETTING + mem0_id present
→ ensure DELETE job

FORGOTTEN + mem0_id present
→ ensure DELETE job + health warning

stale RUNNING job
→ reset PENDING

duplicate index_key vectors
→ select canonical + delete duplicates
```

重建只读取 EAGLE SQLite 的 ACTIVE Preference/Knowledge，不读取 Mem0 history 反推权威状态。

---

## 33. API

新增：

```text
eagle_os_agent/eagle/api/main.py
eagle_os_agent/eagle/api/episodes.py
eagle_os_agent/eagle/api/pack.py
eagle_os_agent/eagle/api/memories.py
eagle_os_agent/eagle/api/forgetting.py
eagle_os_agent/eagle/api/health.py
```

最小接口：

```text
POST /episodes
POST /pack
GET  /preferences
GET  /knowledge
GET  /memories/{kind}/{id}/evidence
POST /memories/forget
POST /preferences/{id}/revoke
GET  /health
GET  /capabilities
```

`POST /episodes` 只写 EAGLE SQLite 和 IndexJob，不直接调用 Mem0。

除 `/health`、`/capabilities` 外，接口从必需的认证依赖取得 `user_id`，业务 payload 不允许自行选择 tenant。`POST /episodes` 必须携带调用方生成的 `execution_id`；同一用户重放相同 ID/内容返回原结果，复用 ID 提交不同内容返回冲突。

`POST /pack` 返回：

```json
{
  "constraints": {},
  "knowledge": [],
  "evidence": []
}
```

`GET /health` 至少暴露：

```text
database_ready
mem0_ready
kylin_embedding_ready
kylin_vector_ready
pending_jobs
failed_jobs
reconciliation_required
```

---

# 第三部分：测试驱动实施顺序

## 34. 第一批测试：治理不变量

先写以下测试，再实现 EAGLE Service：

```python
def test_fallback_does_not_become_preference():
    ...


def test_two_implicit_choices_stay_pending():
    ...


def test_third_choice_across_sessions_commits():
    ...


def test_explicit_future_preference_commits_once():
    ...


def test_hard_preference_physically_filters_tools():
    ...


def test_forgetting_memory_is_not_returned_before_vector_delete():
    ...


def test_committed_memory_has_evidence():
    ...
```

---

## 35. 第二批测试：Mem0 Provider

### Embedding

```text
single embed
batch embed order
batch result count
dimension mismatch
SDK exception propagation
factory construction
```

### VectorStore

```text
collection initialization
insert ID and payload
search filter translation
score normalization
get not-found
delete
metadata-only update
list flat result
reset
unsupported filter failure
factory construction
```

### NoopLLM

```text
factory construction
generate_response always raises
```

测试 mock 麒麟 SDK，不 mock 正在测试的 Provider。

---

## 36. 第三批测试：Mem0 端到端

使用 fake Kylin clients：

```text
Memory.from_config(kylin, kylin, noop)
Memory.add(raw, infer=False)
Memory.search(filters={user_id, memory_kind})
Memory.delete(mem0_id)
infer=True → NoopLLM error
different user → no result
index_key → exact reconciliation lookup
```

重点断言：`infer=False` 对单条 Memory 只执行一次 embedding。

---

## 37. 第四批测试：Outbox 故障注入

```text
EAGLE commit 前退出
→ 无正式 Memory、无 job

EAGLE commit 后 Worker 前退出
→ job 可恢复

Mem0 add 后回填前退出
→ index_key 找回，不持续复制

delete 失败
→ status 保持 FORGETTING

stale vector 返回
→ PACK SQLite intersection 丢弃

duplicate vectors 返回
→ PACK 去重，reconciliation 删除副本
```

---

## 38. 第五批测试：真实麒麟 SDK

只有拿到真实 SDK 后执行：

```text
FFI/import smoke test
single embedding
batch embedding
dimension
concurrent calls
collection lifecycle
payload round trip
filter capability matrix
similarity direction
read-after-write latency
read-after-delete latency
large top_k
pagination
```

测试结果写入 `/capabilities` 的静态支持表或启动探测，不靠文档声明能力。

---

## 39. 实施阶段与提交粒度

### Phase 1：锁定 Mem0 基线

修改：无业务代码。

产物：

```text
commit pin
build instructions
existing smoke-test record
```

验收：当前 commit 可重复构建。

### Phase 2：NoopLLM

修改：

```text
mem0/llms/noop.py
mem0/llms/configs.py
mem0/utils/factory.py
tests/llms/test_noop.py
```

验收：Factory 能构建；调用立即失败。

### Phase 3：Kylin Provider Contract

修改：Embedding、VectorStore 配置、Factory 和 fake-client tests。

验收：不修改 `main.py`，Mem0 add/search/delete 集成测试通过。

### Phase 4：EAGLE Schema

修改：ORM、Alembic、Repository。

验收：migration、唯一约束、Evidence replay 测试通过。

### Phase 5：Episode → Attribution → Gate

修改：Collector、Attributor、Candidate Service、Gate。

验收：Fallback Non-Promotion 和晋升阈值测试通过。

### Phase 6：Commit + Outbox

修改：Preference/Knowledge Service、Gateway、Worker、Reconciliation。

验收：PENDING 不索引，COMMITTED 可收敛索引，故障注入通过。

### Phase 7：PACK

修改：Resolver、Compiler、Filters、Ranking。

验收：HARD violation 为 0；FORGETTING leakage 为 0。

### Phase 8：真实麒麟接入

修改：两个 Kylin SDK adapter 和最终 config。

验收：能力探测、tenant isolation、并发和一致性测试通过。

### Phase 9：Conflict、Revalidation、Forget

修改：治理增强模块。

验收：P-P/K-K/P-K、环境漂移和物理删除场景通过。

---

## 40. 不修改项

第一阶段明确不修改：

```text
mem0/memory/main.py
mem0/memory/storage.py
Mem0 infer=True pipeline
Mem0 hybrid scoring implementation
Mem0 entity extraction implementation
```

也不新增：

- 无验证集支撑的加权总分；
- 自动 Provider fallback；
- Candidate 直接向量化；
- HARD Preference prompt-only enforcement；
- 把 Mem0 history 当作 EAGLE 权威数据库的同步逻辑。

---

## 41. 麒麟 SDK 开发前置条件

真实 Provider 开发前必须确认：

1. Python 包名或动态库加载方式；
2. SDK 版本；
3. client 初始化与凭据方式；
4. 同步/异步与线程安全性；
5. Embedding 模型和维度；
6. batch 上限及输出顺序；
7. Vector metric 与返回 score/distance 定义；
8. collection API；
9. insert/upsert 语义；
10. payload 类型和大小限制；
11. EQ/IN/NOT/AND 和 ID allowlist 能力；
12. list/pagination；
13. get 能否返回 vector；
14. metadata-only update；
15. read-after-write/delete 一致性。

缺少这些事实时，只实现 adapter protocol 和 fake-client contract test，不编造 SDK 调用。

---

## 42. 最终代码调用链

```text
Request
  │
  ▼
PACK Service
  ├── SQLite ACTIVE Preferences
  ├── Scene Resolver
  ├── HARD Constraint Compiler
  ├── Physical Tool Filter
  └── Mem0Gateway.search_knowledge
          │
          ▼
      Memory.search(
        filters={
          user_id,
          memory_kind="K"
        }
      )
          │
          ▼
      KylinEmbedding
          │
          ▼
      KylinVectorStore
          │
          ▼
      SQLite ACTIVE Intersection
          │
          ▼
      Visibility / Environment / Soft Ranking
  │
  ▼
Planner
  │
  ▼
Constrained Tool Executor
  │
  ▼
Episode Collector
  │
  ▼
Attribution
  │
  ▼
Candidate Service
  │
  ▼
Commitment Gate
  ├── PENDING
  ├── DEFER
  ├── REJECTED
  └── COMMITTED
          │
          ▼
      EAGLE SQLite Transaction
      + MemoryEvidenceLink
      + IndexJob
          │
          ▼
      Outbox Worker
          │
          ▼
      Mem0Gateway.index_knowledge
          │
          ▼
      Memory.add(..., infer=False)
```

---

## 43. 最终实施结论

代码改造分为两部分：

```text
Mem0 fork
→ 只扩展 KylinEmbedding、KylinVectorStore、NoopLLM 及其配置/Factory

EAGLE subproject
→ 独立实现全部治理状态和业务规则
```

最重要的工程规则是：

> SQLite 决定 Memory 是否存在、是否 ACTIVE、是否可见；Mem0/麒麟只负责保存和检索派生语义索引。任何 Mem0 搜索结果在进入 Planner 前，都必须回到 SQLite 做权威状态校验。

---

## 44. 本次实现验证记录

已通过（2026-09-06 复核，含 lease reset、duplicate cleanup 与 capability probe 测试）：

```text
EAGLE governance / PACK / outbox / health / reconciliation / capabilities tests
49 passed

Mem0 Kylin providers / factory / raw Memory integration
13 passed
```

统计口径：`pytest tests -q`（`eagle_os_agent/tests`，含 HTTP 认证、幂等、环境隔离、遗忘擦除等 49 个测试函数）与
`pytest tests/embeddings/test_kylin_embedding.py tests/vector_stores/test_kylin_vector_store.py
tests/llms/test_noop.py tests/utils/test_factory.py tests/memory/test_kylin_raw_memory.py -q`
（13 个测试函数）。HTTP API 使用项目 `api` extra 动态验证。

包布局复核：`adapters`/`api` 已并入 `eagle` 顶层包（`eagle.adapters`、`eagle.api`），wheel 只发布单一顶层包，消除与其他包的导入名冲突风险。

端到端测试覆盖：

```text
Memory.from_config(kylin + kylin + noop)
→ Memory.add(infer=False)
→ Memory.search(filters={user_id})
→ Memory.delete
```

同时验证 `infer=True` 会由 NoopLLM 快速失败，并由 Mem0 包装为 `LLMError` 暴露给调用方。

当前环境 `ruff` 与 FastAPI 均已可用（`ruff check` 全绿，HTTP 层由 `fastapi.testclient` 覆盖）；`hatch` 仍不可用，完整 Mem0 测试套件仍有约 35 个用例因无关上游依赖在收集阶段报错，不属于本方案口径。真实麒麟 SDK 验证仍以第 41 节列出的接口事实为前置条件。
