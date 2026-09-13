# EAGLE 用户手册

> 适用版本 `cca864c` · 面向运维/集成/评测三类读者 · 配套 `TECHNICAL_DOCUMENTATION.md` 与 `EFFECT_VERIFICATION_REPORT.md`

---

## 1. 快速导航

| 你是 | 看这里 |
|---|---|
| 首次部署 | §2 环境要求 → §3 安装 → §4 一键校验 |
| 集成到 Agent | §5 核心操作（Governance/PACK/遗忘/健康） |
| 对接 HTTP 网关 | §6 API 网关 |
| 复现论文数据 | §7 评测与复跑 |
| 排障 | §8 常见问题 |

---

## 2. 环境要求

| 项 | 要求 | 备注 |
|---|---|---|
| OS | Ubuntu 24.04 noble / Linux 5.15+ | 验证 `5.15.0-161-generic` |
| Python | 3.11.15 | `/opt/conda` |
| DB | SQLite `:memory:`（单测/评测） | 生产可替换为持久 SQLite/PostgreSQL |
| 可选真麒麟 SDK | `kylin-ai-runtime` D-Bus socket `/tmp/.kylin-ai-runtime-unix/<uid>/core-textembedding.sock`（嵌入 tier 优先探测） | 运行时可达 → `backend=kylin-sdk`；否则 ONNX 兜底 |
| 可选真实向量 | `kylin-ai-model-service` ONNX gte-base 768 (~400MB)（`KYLIN_EMBEDDING_MODEL` 指向） | `Shim` 默认无需 |
| 可选向量引擎 | `kylin-ai-vector-engine 1.2.0.1` UDS `/tmp/kylin-ai-vector-engine-0.sock` Milbus Lite 32MB + antlr4.9.2 `/opt/antlr49` + `/opt/boost190-libs/libboostshim.so` | 无则自动用 `ShimVector` |
| 工具 | `ruff, pytest, git` | - |

---

## 3. 安装

```bash
git clone <repo> && cd EAGLE/eagle_os_agent
pip install -e .  # 读取 pyproject.toml: sqlalchemy, pydantic, mem0, ruff, pytest

# 可选：校验 Shim 能力（11探针）
python -c "from eagle.adapters.kylin.capabilities import probe_vector_capabilities; from eagle.adapters.kylin.vector_shim import ShimVectorClient; print(probe_vector_capabilities(ShimVectorClient(),768,'cosine_distance').supported)"
# 预期：{'eq','in','ne','not','and','id_allowlist','id_blocklist','list_filter','read_after_write','delete'}
```

真实引擎（如需）仅需确认 UDS 与动态库存在，无需改代码（替换点唯一 `eagle/adapters/kylin/shims.py`）：

```bash
ls -l /tmp/kylin-ai-vector-engine-0.sock
ls -l /opt/kylin-vector-engine /opt/antlr49 /opt/boost190-libs/libboostshim.so
ps aux | grep kylin-ai-vector-engine | grep -v grep
```

---

## 4. 一键校验（离线可跑）

```bash
ruff check eagle tests && ruff format --check eagle tests && python -m compileall -q eagle && git diff --check
pytest -q tests/test_governance.py tests/test_preference_resolver.py tests/test_knowledge_revalidation.py
pytest -q tests/test_pack_and_forgetting.py
pytest -q tests/test_outbox.py tests/test_reconciliation.py tests/test_health.py
pytest -q tests/test_api.py
MEM0_TELEMETRY=False pytest -q tests/embeddings/test_kylin_embedding.py tests/vector_stores/test_kylin_vector_store.py tests/llms/test_noop.py tests/utils/test_factory.py tests/memory/test_kylin_raw_memory.py
pytest -q  # 预期 49 passed
python experiments/e2e_stage9/runner.py          # 4/4 PASS
python experiments/stage10/runner.py             # 8/8 PASS
python experiments/stage11_12/runner.py --seeds 42,43,44 --total 200
python experiments/governance_tax_cases.py       # 3/3 PASS
python experiments/governance_tax_v2.py          # 3×350=1050/mode
```

产物：`experiments/*.report.json  FINAL_REPORT.md  GOVERNANCE_TAX_REPORT.md  CONDITIONS.md`

---

## 5. 核心操作

### 5.1 初始化

```python
from eagle.db import create_schema, create_sqlite_engine, make_session_factory
from eagle.adapters.kylin.embedding_shim import ShimEmbeddingClient
from eagle.adapters.kylin.vector_shim import ShimVectorClient
from eagle.bootstrap import create_mem0_gateway

engine = create_sqlite_engine("sqlite:///eagle.db")  # 或 :memory:
create_schema(engine)
session_factory = make_session_factory(engine)
gateway = create_mem0_gateway(
    embedding_client=ShimEmbeddingClient(dim=768),
    vector_client=ShimVectorClient(),
    embedding_dims=768, distance_metric="cosine_distance",
    score_semantics="cosine_distance",
    history_db_path="/tmp/eagle_history.db", collection_name="eagle_main"
)
```

真实麒麟只需把 `ShimEmbeddingClient/ShimVectorClient` 换为 `KylinEmbeddingClient/KylinVectorClient`，其余不变。

### 5.2 记录 Episode（治理入口）

```python
from eagle.domain.events import EpisodeInput
from eagle.domain.scene import Scene
from eagle.governance import GovernanceService

gov = GovernanceService(session_factory)
res = gov.record_episode(EpisodeInput(
    user_id="u1", session_id="s1", request_text="edit docx",
    scene=Scene(app="office", task="edit", artifact_type="docx"),
    tool_name="libreoffice", arguments_digest="d-s1",
    success=True, environment_fingerprint="linux:noble:wps-1",
    fallback_from="wps", previous_error_code="E_OPEN", execution_id="s1-fb-1"
))
print(res.candidate_ids, res.committed_memory_ids)
# 同 execution_id 重放返回同一结果（幂等）
```

**字段说明：**

| 字段 | 必填 | 说明 |
|---|---|---|
| `user_id, session_id, request_text, scene, tool_name, arguments_digest, success, environment_fingerprint` | 是 | 基础 |
| `fallback_from, previous_error_code` | 否 | 标记回退，归因判为 `fallback`，不计入偏好 |
| `user_intervention, user_correction` | 否 | 显式纠正，1 次即提交 |
| `execution_id` | 建议 | 幂等键，未传自动 UUID |
| `explicit_preferences` | 否 | `ExplicitPreferenceEvent(key, value, hardness, scene)` 列表 |

**提交规则：** 显式 1 次；隐式需 `≥3 次独立选择 ∧ ≥2 session`（知识额外 `≥2 env`）；`K-K` 单次矛盾 `DEFER` 不下线。

### 5.3 检索 PACK（规划上下文）

```python
from eagle.pack.service import PackService
pack = PackService(session_factory, gateway)
ctx = pack.build(query="fallback", user_id="u1",
    scene=Scene(app="office", task="edit", artifact_type="docx"),
    environment_fingerprint="linux:noble:wps-1", top_k=5)
print(ctx.constraints)  # HARD 编译结果
print(ctx.knowledge)    # tuple[{id,type,content,score}]
print(ctx.evidence)     # 溯源

# 环境漂移：旧 env 知识不返回，且写 KnowledgeRevalidationRecord
# P-K 冲突：被 HARD Preference 遮蔽的知识自动过滤
# 无 eligible 时 ctx.knowledge == ()
```

**Planner 使用：**

```python
from eagle.preference.compiler import apply_constraints
from eagle.domain.constraints import NO_FEASIBLE_ACTION
from eagle.db.orm import Tool  # 示例

feasible = apply_constraints([Tool(name) for name in ["wps","libreoffice","ranger"]], ctx.constraints)
if feasible is NO_FEASIBLE_ACTION:
    # 硬约束不可满足，直接拒执行（fast-fail）
    pass
```

**Two-tier v2（可选）：** `Executable PACK` 严格可执行；`Hints` 非执行仅计数，不读历史 `action`，仅触发 `Revalidation / 确认 / 当前合法集重规划`。详见 `experiments/governance_tax_cases.py`。

### 5.4 遗忘

```python
from eagle.forgetting.service import ForgettingService
from eagle.outbox.worker import IndexWorker

ForgettingService(session_factory).forget_knowledge(knowledge_id, user_id="u1")
# 需 drain Outbox
while IndexWorker(session_factory, gateway).process_next() is not None:
    pass
# 语义：FORGETTING 立即 PACK 不可见但不报 FORGOTTEN；成功后 content="" retrieval_text="" mem0_id=None FORGOTTEN
```

### 5.5 健康与恢复

```python
from eagle.health import HealthService
from eagle.outbox.reconciliation import ReconciliationService

print(HealthService(session_factory, gateway).check().to_dict())
# {status ok/degraded, database_ready, mem0_ready, kylin_embedding/vector_ready, pending/failed_jobs, reconciliation_required, errors}

ReconciliationService(session_factory, gateway).reconcile(lease_seconds=300)
# 陈旧 RUNNING(>300s) 归 PENDING；新鲜不抢占；DONE但向量缺失清 mem0_id 重排 UPSERT
```

### 5.6 Outbox

```python
from eagle.outbox.worker import IndexWorker
worker = IndexWorker(session_factory, gateway, max_retries=3)
job_id = worker.process_next()  # PENDING→RUNNING→DONE/FAILED 指数退避 2^retry
```

故障语义 `stage10 8/8` 验证：`timeout→退避→DONE / 崩溃孤立→reconcile→single canonical / 重复→DELETE_DUPLICATE→1 / Delete失败→FORGETTING+FAILED / 外部删→degraded→reconcile→ok`。

---

## 6. API 网关（如部署 HTTP 层）

| 行为 | 预期 |
|---|---|
| 无 token | 401 |
| 显式传 `user_id` 覆盖 | 拒绝（服务端注入） |
| 跨用户查询 | 空 404 同语义 |
| 同 `execution_id` 重放 | 返回同一结果 |
| `fallback_from` 缺 `previous_error_code` 等 | 422 |
| 正常查询 | `filters{user_id, memory_kind, id:{in:eligible}}` 强制租户隔离 |

---

## 7. 评测与复跑

详见 `docs/EFFECT_VERIFICATION_REPORT.md`。一键：

```bash
python experiments/datasets/eagle-gov/runner.py --seed 42  # Gov 200/200
python experiments/stage11_12/runner.py --seeds 42,43,44 --total 200  # 6000 runs 95%CI
python experiments/governance_tax_v2.py  # v2 1050/mode frozen weighting 200+50*3
```

产物 JSON 均含 `seed / sha256 / 均值±std 95%CI`。

---

## 8. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `PACK 0` 但 Worker 成功 | `threshold` 非 0 误滤 shim 近正交 `≈0.03` | 保持 `gateway.search_knowledge threshold=0`（已修复） |
| `Health degraded` 但无 FAILED | `reconciliation_required` 真漂移 | 执行 `reconcile()` 后重查 |
| `HardViolation` 仍出现 | 用了文本偏好未编译 | 走 `PreferenceCompiler` 双重保障 |
| `Forget 后仍可见` | 未 `drain` 或查旧 env/跨用户 | `drain` 后同 `user_id + env` 复查 |
| `Recall 0.60` 被质疑 | 混淆 `GovernedVisibility` 与 `EligibleRecall` | 看 `EligibleRecall 1.0`（合法零丢失）+ `MissedSafeAction` |

---

## 9. 支持

* 技术文档：`docs/TECHNICAL_DOCUMENTATION.md`
* 验证报告：`docs/EFFECT_VERIFICATION_REPORT.md`
* 总报告：`experiments/FINAL_REPORT.md` `experiments/GOVERNANCE_TAX_REPORT.md`
* 环境快照：`experiments/CONDITIONS.md` `environment_snapshot.txt`
