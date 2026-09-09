# EAGLE 实验条件固化快照（阶段 0）

> 生成时间：2026-09-09 11:20 (+08)
> 生成方式：本文件由脚本从运行环境实测采集，非手填。
> 作用：`EAGLE-experiment.md` 阶段 0 要求在进入任何正式实验前固化下列 9 项条件；
>       后续所有阶段（1-13）必须以本快照记录的环境为准，任何变更需重新固化并注明。

## 1. Git commit

| 项 | 值 |
|---|---|
| 仓库 | `/root/rivermind-data/EAGLE`（remote: https://github.com/cyi53502/EAGLE.git） |
| HEAD commit | `cca864ccf9dd36e0a80cf29c334c9eb52f8b623a`（"v1：基于mem0的修改"，2026-09-07 13:49:42 +0800） |
| Mem0 上游基线 | `9a7924befd7026e41e445ba809370009e5e985a6`（见 EAGLE_MEM0_DESIGN.md / IMPLEMENTATION_PLAN 声明） |
| 本地改动 | 9 个 `eagle_os_agent/tests/*.py` 的 ruff I001 import 排序修复（随本次固化一并提交） |
| 基线 commit 完整性 | `git diff --check` = 0；`git status` 干净后为本文件的固化 commit |

## 2. Python 与依赖版本

| 项 | 值 |
|---|---|
| 解释器 | CPython 3.11.15（`/opt/conda/bin/python`，GCC 14.3.0） |
| 备用 env | conda env `eagle` = CPython 3.10.21（`/opt/conda/envs/eagle`，当前仅 pip/pytest/ruff，无 sqlalchemy） |
| pip | 26.1.1 |
| pytest | 9.1.1 |
| ruff | 0.16.6 |
| sqlalchemy | **未安装（阻塞项）** — `eagle.db` 与全部测试所需 |
| pydantic | 2.13.2 |
| httpx | 0.28.1 |
| numpy | 2.4.6 |
| fastapi / uvicorn | 未安装（阶段 5 API 测试前需装） |
| mem0ai | **未以包形式安装（阻塞项）** — 源码树版本 2.0.20，`importlib.metadata` 找不到元数据，Provider 契约测试（阶段 6）无法 import `mem0` |
| 安装约定 | 正式实验统一使用 `/opt/conda/bin/python`；`pip install -e /root/rivermind-data/EAGLE` 与 `pip install -e /root/rivermind-data/EAGLE/eagle_os_agent`（含 `.[api,test]`） |

## 3. Mem0 commit / 版本

| 项 | 值 |
|---|---|
| 源码位置 | `/root/rivermind-data/EAGLE/mem0`（同一 git 仓库内，非 submodule） |
| 声明版本 | `2.0.20`（`pyproject.toml` name=mem0ai） |
| 修改文件 | `configs/embeddings/kylin.py`、`embeddings/kylin.py`、`configs/vector_stores/kylin.py`、`vector_stores/kylin.py`、`llms/noop.py`、`embeddings/configs.py`、`vector_stores/configs.py`、`llms/configs.py`、`utils/factory.py` |
| 遥测 | `MEM0_TELEMETRY=False`（bootstrap.py 强制，构造前校验，正式实验必须保持） |
| LLM | `noop`（任何 infer=True 调用立即 RuntimeError，正式写入只允许 `infer=False`） |

## 4. 麒麟 SDK / 服务版本

| 项 | 值 |
|---|---|
| 引擎包 | `kylin-ai-vector-engine 1.2.0.1-1+b2`（dpkg 状态 `iU`：已解包未配置 —— 需 `dpkg --configure -a`） |
| 客户端库 | `libkysdk-vector-engine-client 1.2.0.0-1`（dpkg 状态 `ii`） |
| deb 缓存 | `/root/rivermind-data/kylin-ai-vector-engine_1.2.0.1-1+b2_amd64.deb`、`/root/rivermind-data/libkysdk-vector-engine-client_1.2.0.0-1_amd64.deb` |
| 接入边界 | 未编造 SDK 类型；规范化 client 协议定义于 `eagle/adapters/kylin/{embedding_client,vector_client}.py`，能力探测 `probe_vector_capabilities`（`PROBE_FILTER_DIALECT=kylin-normalized-v1`） |
| 当前状态 | **真实 SDK 尚未接入**（设计文档声明：真实 SDK 包名/请求/响应类型待提供，阶段 8 Smoke 前不得启动正式服务） |

## 5. Embedding 模型及维度

| 项 | 值 |
|---|---|
| Provider | `kylin`（`mem0/embeddings/kylin.py`，维度不一致立即 RuntimeError） |
| 维度 | **待阶段 8 探测后回填**（配置参数化 `embedding_dims`，README 示例 1024，测试用 2；正式实验值以真实 SDK smoke 为准） |
| 模型名 | 未指定（`KylinEmbeddingConfig.model` 可选，当前 None，跟随 SDK 默认） |
| 批量 | 支持 `embed_batch`（返回条数必须等于输入条数） |

## 6. Vector distance_metric / score_semantics

| 项 | 值 |
|---|---|
| distance_metric | **待阶段 8 探测确认真实枚举后回填**（当前仅测试值 `cosine_distance`；设计文档明确：未确认前不得把示例枚举提交为生产实现） |
| score_semantics | 三选一：`cosine_distance` / `l2_distance` / `similarity`（`mem0/configs/vector_stores/kylin.py` Literal 强约束）；归一化规则：cosine_distance→`1-d`、l2_distance→`1/(1+d)`、similarity→原值 |
| collection | 默认 `eagle_memories`（正式实验另建专用 collection，不复用测试 collection） |
| 目标不变量 | 归一化后 Mem0 侧 score 必须满足“越大越相似” |

## 7. 随机种子

| 项 | 值 |
|---|---|
| 全局种子 | **统一 `SEED=42`**（本文件固化时刻起生效） |
| Python | 每个实验入口先 `random.seed(42)`、`os.environ["PYTHONHASHSEED"]="42"` |
| NumPy | `numpy.random.seed(42)`（tests/vector_stores 已有先例） |
| SQLite/向量库 | 无随机成分；写序确定 |
| 说明 | 阶段 11/13 的多组重复运行在此外层再循环 SEED ∈ {42, 7, 2026, 3407, 1234} |

## 8. 数据集版本

| 项 | 值 |
|---|---|
| EAGLE-Gov v1 | **已就位（2026-09-09，本地闭环已通过）**：`experiments/datasets/eagle-gov/v1.jsonl`，200 条，sha256 `51b25e270df1309aa57cd4683a9425a3ed533e3b71d0b1a8092d61380a4b8266`，生成 `python experiments/datasets/eagle-gov/generate.py --seed 42`（deterministic，无 LLM/外部依赖）；本地闭环 200/200 pass（`runner.py`，stage-9 前置模式，SearchGateway 直接服务 SQLite 权威行） |
| LongMemEval-V2 | 未拉取（阶段 8 麒麟 Smoke 通过后 `hf download mem0ai/longmemeval-v2`） |
| LongMemEval | 未拉取（同上，`ulab-ai/longmemeval`） |
| LoCoMo | 未拉取（同上，`snap-research/locomo`） |
| OSWorld/openKylin 子集 | 未拉取（同上，`github.com/xlang-ai/OSWorld`，子集清单另记 `SUBSET.md`） |
| 约定 | 数据集就位后在本节登记：名称、来源 URL/路径、下载日期、sha256、条数、切分比例、license |
| 阶段 1-8 | 不依赖外部数据集（EAGLE-Gov 已可先行） |

## 9. import stub 排除声明

- 正式实验（阶段 2 起）**不得**使用任何测试用 import stub / fake Provider 冒充真实依赖。
- 允许的注入仅限两类：(a) `eagle/adapters/kylin/*` 协议背后的**归一化 client 实现**（真实 SDK 或测试 fake，由 DI 显式传入）；(b) pytest fixture 的内存 SQLite。
- `eagle.bootstrap.create_mem0_gateway` 已内置三重防护：`MEM0_TELEMETRY=False` 前置校验、provider 必须 `kylin/kylin/noop`、配置错误不 fallback（`extra="forbid"`）。
- 若发现任何 `sys.modules` 替换式 stub 进入正式路径，实验立即作废并回滚。

## 10. 已知阻塞项（进入阶段 2 前必须解决）

> 2026-09-09 更新：#1/#2/#3/#5 已解决，#4（麒麟 dpkg 配置）仍待处理 —— 阶段 8 前执行 `dpkg --configure -a`。

| # | 阻塞 | 影响 | 处理 |
|---|---|---|---|
| 1 | ~~`eagle_os_agent/eagle/db/` 目录缺失~~ | ~~阶段 2-5、7 全部 ImportError~~ | **已解决**：重建 `eagle/db/{__init__,base,engine,orm}.py`（9 ORM 实体 + engine 三函数；引擎模式 `isolation_level=None` + noop begin event 以支持服务层显式 `BEGIN IMMEDIATE`） |
| 2 | ~~`mem0ai` 元数据缺失~~ | ~~阶段 6/7 收集即失败~~ | **已解决**：`pip install -e . --no-deps` + posthog/qdrant-client/openai/protobuf/pytz 运行时依赖 |
| 3 | ~~`sqlalchemy` 未安装~~ | ~~同 #1~~ | **已解决**：sqlalchemy 2.0.52、fastapi 0.141.1、uvicorn 0.52.4 |
| 4 | `kylin-ai-vector-engine` 未完成 dpkg 配置 | 阶段 8 无法起服务 | `dpkg --configure -a` 后再 smoke（唯一未决项） |
| 5 | ~~`fastapi/uvicorn` 未安装~~ | ~~阶段 5 API 测试~~ | **已解决** |

## 11. 基线数字（来自 EAGLE-experiment.md，固化时点尚未复现验证）

> 2026-09-09 14:30 复现完成：全量/ Provider 均通过汇总见 `experiments/GOV_HARNESS_REPORT.md`。

- 全量本地回归基线：`49 passed`（阶段 7 目标）— **已复现 49 passed**
- Provider 契约基线：`13 passed`（阶段 6 目标）— **已复现 13 passed**
- EAGLE-Gov v1：200/200 pass（9 families，本地闭环无麒麟）— 见 `experiments/datasets/eagle-gov/v1.report.json`
