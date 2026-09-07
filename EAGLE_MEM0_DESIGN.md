# EAGLE：面向 OS Agent 的执行证据驱动长期记忆治理框架

## 理论设计与基于 Mem0 OSS 的代码实现方案

> 文档状态：理论基线（核心治理闭环与 Mem0 Provider 已实现）  
> Mem0 源码基线：`9a7924befd7026e41e445ba809370009e5e985a6`  
> Mem0 Python 包版本：`2.0.20`  
> 分析日期：2026-09-04

---

## 0. 摘要

EAGLE 研究的不是“如何保存更多历史”，而是：

> Agent 如何判断一段历史行为是否有资格长期影响未来决策，以及它应当以什么权限、在什么条件下影响未来行为。

传统长期记忆通常采用：

```text
Observation
→ Memory Extraction
→ Vector Store
→ Retrieval
```

EAGLE 将其改写为：

```text
Execution Episode
→ Evidence Attribution
→ Memory Candidate
→ Commitment Gate
→ Governed Memory
→ Constraint / Retrieval
→ Future Action
```

其核心定义是：

> 长期记忆不是历史记录的压缩，而是经过证据治理后，获得特定未来决策影响权的状态化经验。

在工程上，本方案采用以下边界：

```text
Mem0 OSS Python
= 底层语义记忆代码基座

EAGLE
= 外部策略治理层

EAGLE SQLite
= Preference、Knowledge、Evidence、Version、Lifecycle 的权威状态

Mem0 + Kylin Embedding + Kylin VectorStore
= 可丢弃、可重建的派生语义索引
```

EAGLE 不修改 `mem0/memory/main.py` 的决策流程。只有经 Commitment Gate 判定为 `COMMITTED` 的 Preference 或 Knowledge，才由 Outbox Worker 调用：

```python
memory.add(..., infer=False)
```

这使 EAGLE 成为唯一治理中心，Mem0 负责 embedding、向量存取和统一的 Memory API。

---

# 第一部分：理论设计

## 1. 问题定义

### 1.1 目标

设 Agent 在时刻 $t$ 观察到一次执行过程 $E_t$。EAGLE 需要回答四个问题：

1. 这次执行提供了什么证据？
2. 证据指向用户偏好，还是条件化执行知识？
3. 证据是否足以让候选记忆长期生效？
4. 已生效记忆应约束动作空间，还是只参与语义召回与排序？

因此，EAGLE 的优化目标不是 Memory Quantity，而是：

$$
MemoryCorrectness
+ MemoryApplicability
+ DecisionSafety
+ Traceability
$$

### 1.2 四个基本区分

```text
行为 ≠ 偏好
成功 ≠ 稳定知识
知识有效 ≠ 当前可见
记住 ≠ 永久有效
```

这些区分决定了 EAGLE 必须位于执行层与长期语义索引之间，而不能只是一个新的 prompt 模板。

---

## 2. 基本理论假设

### 2.1 行为与偏好不可直接等价

观察到动作 $a_t$，不能直接推出该动作是用户偏好：

$$
ObservedAction(a_t) \not\Rightarrow UserPreference(a_t)
$$

因为动作可能来自用户显式选择、Agent 自主决策、系统默认值、工具失败后的回退、环境限制或偶然成功。

### 2.2 成功与长期知识不可直接等价

单次成功不代表稳定知识：

$$
Success(a_t) \neq StableKnowledge(a_t)
$$

只有当执行策略具有足够证据，并明确适用条件时，才能晋升为长期 Knowledge。

### 2.3 Preference 与 Knowledge 的权限不同

Preference 表示“用户希望系统如何行动”，Knowledge 表示“系统过去学到了什么”。因此：

$$
Authority(P) > Applicability(K)
$$

发生 P-K 冲突时，Preference 决定 Knowledge 当前是否可见；Knowledge 不得反向覆盖 Preference。

### 2.4 Knowledge 的有效性依赖环境

OS Agent 的经验通常是条件化的：

$$
K = (Condition, Action, Evidence)
$$

其中：

$$
Condition = Scene + Environment + ToolVersion + ToolSchema
$$

所以知识有效性应表示为：

$$
Valid(K \mid Condition)
$$

而不是无条件的 $Valid(K)$。

### 2.5 长期记忆必须可撤销

Memory 必须支持完整生命周期：

```text
Create
→ Validate
→ Use
→ Mask
→ Revalidate
→ Revoke
→ Forget
```

Memory 是动态状态，不是永久静态文本。

---

## 3. 五层理论模型

```text
L1 Execution Layer
        ↓
L2 Evidence Layer
        ↓
L3 Memory Governance Layer
        ↓
L4 Memory State Layer
        ↓
L5 Decision Influence Layer
```

### 3.1 L1：Execution Layer

捕获真实工具调用、结果、错误、回退、用户干预与环境状态。

### 3.2 L2：Evidence Layer

将 Episode 归因为 Preference、Knowledge、Failure、Environment Drift 或 Conflict 证据。

### 3.3 L3：Memory Governance Layer

聚合 Evidence，形成 Candidate，并通过 Commitment Gate 决定状态转移。

### 3.4 L4：Memory State Layer

维护 Preference、Knowledge、版本链、可见性、重验证与遗忘状态。

### 3.5 L5：Decision Influence Layer

HARD Preference 编译为物理动作约束；SOFT Preference 参与排序；Knowledge 在通过可见性和环境校验后进入 PlannerContext。

---

## 4. Execution Episode

定义一次 Agent 与环境交互的执行单元：

$$
E_t = (Q_t, C_t, A_t, R_t, U_t, Env_t)
$$

其中：

- $Q_t$：用户请求；
- $C_t$：当前 Scene；
- $A_t$：Agent 执行动作；
- $R_t$：执行结果；
- $U_t$：用户反馈、干预或纠正；
- $Env_t$：环境状态。

工程上的最小充分记录为：

```text
request
scene
tool
arguments_digest
success
result_class
error_code
latency_ms
retry_count
fallback_from
previous_error_code
user_intervention
user_correction
environment_fingerprint
```

Episode 不直接形成长期记忆；它只保存能够支持归因的事实证据。

---

## 5. Evidence 与归因

### 5.1 Evidence 定义

$$
e_i = (type, direction, strength, condition, source)
$$

其中：

- `type`：`PREFERENCE | KNOWLEDGE | FAILURE | ENV_DRIFT | CONFLICT`；
- `direction`：`POSITIVE | NEGATIVE | NEUTRAL`；
- `strength`：`STRONG | WEAK`；
- `condition`：证据成立的 Scene 和 Environment；
- `source`：来源 Episode。

### 5.2 Attribution 函数

$$
A(E_t) \rightarrow \{e_1, e_2, \ldots, e_n\}
$$

Attribution 回答“这次执行究竟说明了什么”，而不是“应该把哪段文本写入向量库”。

### 5.3 显式用户声明

例如：

```text
以后编辑 DOCX 都使用 WPS。
```

产生强正向 Preference Evidence：

$$
e_P^{strong+}
$$

显式声明通常一次即可达到提交资格，但同场景存在未解决语义冲突时应进入 `DEFER`。

### 5.4 隐式稳定选择

在存在真实可选项时，多次独立选择同一工具：

```text
WPS → WPS → WPS
```

产生弱正向 Preference Evidence：

$$
e_P^{weak+}
$$

它必须跨时间、跨 session 累积，避免把默认值或偶然行为提升为偏好。

### 5.5 Fallback 归因

当：

```text
A failed
→ B succeeded
```

应产生条件化 Knowledge Evidence：

$$
e_K^+
$$

对应知识：

$$
(A, Error, Env) \rightarrow B
$$

核心不变量为：

$$
FallbackSuccess \Rightarrow KnowledgeEvidence
$$

$$
FallbackSuccess \not\Rightarrow PreferenceEvidence
$$

---

## 6. Memory Candidate 与 Commitment Gate

### 6.1 Candidate

Evidence 先聚合为候选：

$$
M_c = (type, key, value, scene, E^+, E^-, confidence)
$$

状态集合：

```text
PENDING
COMMITTED
REJECTED
DEFER
```

Candidate 的作用是建立“观察到规律”和“允许规律长期影响决策”之间的缓冲区。

### 6.2 Candidate Identity

同类证据通过稳定身份聚合：

```text
SHA-256(
  user_id
  + candidate_type
  + candidate_key
  + canonical_json(candidate_value)
  + canonical_json(normalized_scene)
)
```

数据库必须对 `candidate_identity` 建唯一索引。`canonical_json` 应固定键排序和紧凑分隔符；Scene 中的空值在计算前删除。

### 6.3 Gate 函数

$$
G(M_c, E) \rightarrow S
$$

其中：

$$
S \in \{PENDING, COMMITTED, REJECTED, DEFER\}
$$

第一阶段采用透明规则，不使用没有验证集支撑的加权总分。

### 6.4 Preference 晋升规则

显式 Preference：

```text
explicit_user_statement = true
AND unresolved_conflict = false
→ COMMITTED
```

隐式 Preference：

$$
independentChoices \ge 3
$$

且：

$$
distinctSessions \ge 2
$$

且：

$$
negativeEvidence = 0
$$

才允许从 `PENDING` 进入 `COMMITTED`。

### 6.5 Workflow Knowledge 晋升规则

$$
sameConditionSuccess \ge 2
$$

或：

$$
sameConditionSuccess = 1 \land UserConfirmed = true
$$

才允许提交。

---

## 7. Preference / Knowledge 双系统

长期记忆集合为：

$$
M = P \cup K
$$

### 7.1 Preference

$$
P = (Rule, Scope, Hardness, Confidence)
$$

Preference 描述用户希望 Agent 如何行动。

### 7.2 Knowledge

$$
K = (Condition, Action, Evidence, Confidence)
$$

Knowledge 描述系统在什么条件下学到了什么。

### 7.3 Hardness

Preference 分为：

- `HARD`：具有动作约束权限；
- `SOFT`：只影响合法动作或知识的排序。

例如“敏感文档禁止联网”必须被编译为结构化约束，而不是仅附加到 prompt。

---

## 8. Constraint Compilation

传统 Memory 通常是：

$$
Memory \rightarrow Prompt
$$

EAGLE 对 HARD Preference 使用：

$$
P_H \rightarrow ConstraintCompiler \rightarrow FeasibleActionSpace
$$

设全体动作集合为 $A$，编译后的合法动作集合为：

$$
A' = \{a \in A \mid a \models P_H\}
$$

因此：

$$
A' \subseteq A
$$

Planner 只能在 $A'$ 中决策。HARD Preference 由此成为 Agent 行为控制变量，而不是一段可能被模型忽略的语言提示。

---

## 9. PACK

定义：

$$
PACK(Q, C, Env) = (Constraints, Knowledge, Evidence)
$$

正式执行顺序为：

```text
Query + Scene + Environment
        │
        ▼
SQLite 查询 ACTIVE Preference
        │
        ▼
Scene Match
        │
        ▼
Version Collapse
        │
        ▼
HARD Constraint Compilation
        │
        ▼
Mem0.search 召回 Knowledge 候选
        │
        ▼
SQLite 权威状态交集
        │
        ▼
P-K Visibility
        │
        ▼
Environment Validation
        │
        ▼
SOFT Preference Rerank
        │
        ▼
PlannerContext
```

这里的“SQLite 权威状态交集”不可省略。即使向量 metadata 仍写着 `ACTIVE`，只要 SQLite 已进入 `FORGETTING`、`REVOKED` 或 `NEEDS_REVALIDATION`，该结果就不得进入 Planner。

### 9.1 Scene Scope

每条 Memory 具有作用域 $Scope(M)$。定义 Specificity 为非空 Scene 条件数。

若多个同 key Preference 同时匹配，则选择：

$$
\operatorname*{arg\,max}_{P} Specificity(P)
$$

即更具体规则覆盖一般规则。

### 9.2 Knowledge Retrieval

Knowledge 先通过向量召回得到候选集 $K_c$，再经过确定性过滤：

```text
Tenant Scope
→ SQLite ACTIVE Intersection
→ Scene
→ P-K Visibility
→ Environment
→ Soft Ranking
```

最终得到 $K^*$ 并进入 Planner。

### 9.3 第一阶段排序原则

不在没有验证数据时定义伪精确权重：

$$
Score = w_1 semantic + w_2 preference + w_3 evidence + w_4 scene
$$

第一阶段采用层级规则：

```text
Hard Constraint
> Tenant / Lifecycle Safety
> Scene Applicability
> Semantic Relevance
> Preference Compatibility
> Evidence Quality
```

获得验证集后，才评估是否学习权重。

---

## 10. 冲突、有效性与可见性

冲突空间为：

$$
Conflict = C_{PP} \cup C_{KK} \cup C_{PK}
$$

### 10.1 P-P Conflict

同 user、同 key、同 Scope、不同 value 构成 P-P 冲突。

明确的新声明产生新版本：

```text
P_old.status → REVOKED
P_new.status → ACTIVE
P_new.parent_version_id = P_old.id
```

不同 Scene 的 Preference 可同时存在；无法确定 Scene 或新旧关系时进入 `DEFER`。

### 10.2 K-K Conflict

Knowledge 冲突必须比较 Condition，不能只比较文本。

- 环境不同：两条 Knowledge 可同时有效；
- 环境相同、条件相同但动作或结果矛盾：进入 Revalidation；
- 验证成功：创建新版本，不覆盖旧版本内容。

### 10.3 P-K Conflict

当 Preference 禁止某类动作，而 Knowledge 建议该动作时，不删除 Knowledge，而是建立可逆 Visibility Mask：

$$
Visible(K \mid P) = False
$$

Preference 撤销后，Mask 可失活，Knowledge 恢复可见。

### 10.4 Validity 与 Visibility 分离

$$
Valid(K) = True
$$

并不意味着：

$$
Visible(K, C, P) = True
$$

Validity 描述 Knowledge 本身是否可信；Visibility 描述当前上下文是否允许使用。

---

## 11. Environment Revalidation

Environment 至少包含：

```text
OS
OS version
application version
tool version
tool schema hash
network class
relevant capability flags
```

每条条件化 Knowledge 保存其 `environment_fingerprint`。当当前环境不匹配时，不直接判定知识为假，而是：

```text
ACTIVE → NEEDS_REVALIDATION
```

第一阶段采用安全策略：未重验证前不返回该 Knowledge。

验证函数：

$$
V(K, Env') \rightarrow \{SUCCESS, FAIL, UNKNOWN\}
$$

- `SUCCESS`：创建适用于新环境的新版本；
- `FAIL`：旧知识不适用于当前环境；
- `UNKNOWN`：保持不可用于高可信执行。

---

## 12. Version 与 Forgetting

### 12.1 不可变内容版本

正式 Memory 的内容采用版本链：

$$
M_1 \rightarrow M_2 \rightarrow M_3
$$

新内容通过新行和 `parent_version_id` 表达，不原地覆盖旧内容。生命周期状态允许变更，但 key、value/content、Scene、Environment 等版本内容保持不可变。

### 12.2 逻辑遗忘

用户发起遗忘后，在 EAGLE SQLite 事务内立即执行：

```text
ACTIVE → FORGETTING
create IndexJob(DELETE)
```

PACK 只接受 `ACTIVE`，因此提交事务后即满足：

$$
Recall(M) = False
$$

### 12.3 物理擦除

Worker 删除 Mem0/麒麟索引并完成所需关联清理后：

```text
FORGETTING → FORGOTTEN
mem0_id → NULL
```

删除失败时必须保持 `FORGETTING` 并暴露失败状态，不能报告“已彻底删除”。

需要预先定义产品语义：普通 `revoke` 可保留审计证据；用户要求的隐私性 `forget` 应清除可识别内容、Evidence Link 和派生索引，只保留不含原文的最小操作 tombstone（如法规允许且确有需要）。

---

## 13. 系统不变量

| 编号 | 不变量 | 可执行断言 |
|---|---|---|
| I1 | Non-Promotion | Fallback 成功不得产生 P Candidate |
| I2 | Commitment Isolation | `PENDING` Candidate 不得进入 Preference/Knowledge 和 Mem0 |
| I3 | Preference Priority | P-K 冲突时由 P 决定 K 的 Visibility |
| I4 | Hard Constraint Safety | 不满足 HARD P 的工具不进入 Planner action space |
| I5 | Environment Safety | 环境不匹配且未重验证的 K 不进入 PlannerContext |
| I6 | Forget Safety | `FORGETTING/FORGOTTEN` 的 Memory 不得被 PACK 返回 |
| I7 | Traceability | 每条 `COMMITTED` Memory 至少关联一条 Evidence |
| I8 | Single Authority | EAGLE 正式写入 Mem0 时必须使用 `infer=False` |
| I9 | Tenant Isolation | 所有 Mem0 查询必须包含 EAGLE 注入的 tenant scope |
| I10 | Derived Index | 删除整个 Mem0/Vector 索引后可从 SQLite 重建 ACTIVE 数据 |

---

## 14. 研究假设与指标

### 14.1 可验证假设

- **H1**：EAGLE 能降低 Preference False Promotion Rate。
- **H2**：HARD Preference Constraint Compilation 能降低 Hard Constraint Violation Rate。
- **H3**：Environment Revalidation 能降低 Stale Knowledge Reuse Rate。
- **H4**：P-K Visibility Mask 能在保持 Knowledge Retention 的同时降低 Preference Conflict Violation。
- **H5**：Evidence Gate 能提高 Memory Precision，并最终提升 Downstream Task Success。

### 14.2 指标

Preference Precision：

$$
\frac{CorrectCommittedP}{AllCommittedP}
$$

False Promotion Rate：

$$
\frac{NonPreferencePromotedToP}{AllNonPreferenceCases}
$$

Traceability Coverage：

$$
\frac{CommittedMemoryWithEvidence}{AllCommittedMemory}
$$

目标为 $100\%$。

同时评估：

- Knowledge Precision；
- Knowledge Recall@K；
- Conflict Resolution Accuracy；
- Hard Constraint Violation Rate；
- Stale Knowledge Reuse Rate；
- Logical Forget Leakage Rate；
- Physical Residue Rate；
- Downstream Task Success；
- Candidate Commit Latency。

---

## 15. 理论核心公式

长期记忆状态更新：

$$
M_{t+1} = G(M_t, A(E_t), C_t, Env_t, U_t)
$$

Agent 决策：

$$
Action_t = \pi\left(
Q_t,
Compile(P_t),
Retrieve(K_t \mid C_t, Env_t, P_t)
\right)
$$

即：

```text
Action = Planner(
  Request,
  PreferenceConstraints,
  ApplicableKnowledge
)
```

---

# 第二部分：当前 Mem0 源码分析

## 16. 分析范围与结论

本节只描述 commit `9a7924befd7026e41e445ba809370009e5e985a6` 的实际代码，不把其他版本文档或旧实现当作当前事实。

结论如下：

1. `Memory.add(..., infer=False)` 确实绕过 LLM extraction/update/delete 决策，逐条消息 embedding 后直接写入 VectorStore。
2. `Memory.__init__` 无论是否使用 `infer=False`，都会实例化 Embedder、VectorStore 和 LLM；因此 NoopLLM 有实际价值。
3. 当前 `Memory.search` 必须使用 `filters={"user_id": ...}`；顶层 `user_id=` 会被拒绝。
4. 当前 `Memory.search` 不只是一次向量搜索，还包含语义过采样、可选 keyword search、实体增强和统一重排。
5. Kylin VectorStore 必须实现 `VectorStoreBase` 的全部抽象方法，且返回“越大越相似”的 score。
6. 当前 Provider 注册不是自动发现。Embedding、VectorStore、LLM 分别需要修改配置白名单/映射和 Factory。
7. Mem0 自带的 SQLite 仅保存 Mem0 history/messages，不足以承载 EAGLE 权威状态。
8. Mem0 的向量写入与其 history SQLite 写入也不是一个跨存储事务，因此 EAGLE 仍必须使用 Outbox 和 Reconciliation。

### 16.1 源码证据索引

| 结论 | 当前 commit 的源码位置 |
|---|---|
| `Memory` 无条件构造 Embedder、VectorStore、LLM | [`mem0/memory/main.py`](mem0/memory/main.py#L487-L501) |
| `Memory.from_config` 使用 `MemoryConfig` 校验 | [`mem0/memory/main.py`](mem0/memory/main.py#L730-L737) |
| `add` 的当前签名与 `infer=True` 默认值 | [`mem0/memory/main.py`](mem0/memory/main.py#L760-L797) |
| `infer=False` 逐消息 raw insert | [`mem0/memory/main.py`](mem0/memory/main.py#L879-L914) |
| `_create_memory` 自动生成 UUID、payload 并先写向量 | [`mem0/memory/main.py`](mem0/memory/main.py#L1961-L1991) |
| `search` 必须通过 `filters` 提供 entity scope | [`mem0/memory/main.py`](mem0/memory/main.py#L1379-L1461) |
| 搜索包含过采样、keyword 与 entity boost | [`mem0/memory/main.py`](mem0/memory/main.py#L1628-L1731) |
| `delete` 先 get，不存在时抛错 | [`mem0/memory/main.py`](mem0/memory/main.py#L1869-L1888) |
| `VectorStoreBase` 完整抽象与 score 方向 | [`mem0/vector_stores/base.py`](mem0/vector_stores/base.py#L4-L100) |
| Embedder/VectorStore/LLM 的实际 Factory 映射 | [`mem0/utils/factory.py`](mem0/utils/factory.py#L35-L223) |
| Embedder provider 白名单 | [`mem0/embeddings/configs.py`](mem0/embeddings/configs.py#L6-L31) |
| VectorStore 动态配置类映射 | [`mem0/vector_stores/configs.py`](mem0/vector_stores/configs.py#L6-L68) |
| LLM provider 白名单 | [`mem0/llms/configs.py`](mem0/llms/configs.py#L6-L35) |
| Mem0 SQLite 只创建 history/messages | [`mem0/memory/storage.py`](mem0/memory/storage.py#L11-L19) |
| `AsyncMemory` 通过线程调用同步 provider | [`mem0/memory/main.py`](mem0/memory/main.py#L3293-L3313) |
| 当前 wheel 只打包 `mem0` | [`pyproject.toml`](pyproject.toml#L92-L105) |

这些链接用于本仓库内审阅。升级 Mem0 commit 后，应重新执行本节核验；行号和行为都不应被视为跨版本稳定契约。

---

## 17. `Memory` 初始化路径

`mem0/memory/main.py` 中，`Memory.__init__` 的顺序是：

```text
EmbedderFactory.create
→ VectorStoreFactory.create
→ LlmFactory.create
→ SQLiteManager(history_db_path)
→ optional telemetry vector store
```

这意味着：

- 即使所有生产写入都固定 `infer=False`，默认 OpenAI LLM 仍会在初始化时被构造；
- 如果没有显式设置 NoopLLM，系统可能仍要求默认 LLM 的配置或凭据；
- `MEM0_TELEMETRY` 默认开启，初始化时可能创建第二个 VectorStore collection：`mem0migrations`；
- Entity Store 是懒加载的，搜索抽取到实体时可能创建 `${collection_name}_entities`。

建议 EAGLE 生产环境显式设置：

```bash
MEM0_TELEMETRY=False
```

这不是正确性的必要条件，但能避免麒麟侧出现非业务 collection。若保留 telemetry，Kylin Provider 必须支持同一配置派生不同 collection。

---

## 18. `infer=False` 的准确行为

同步路径位于 `Memory._add_to_vector_store`：

```text
for each non-system message
→ copy metadata
→ attach role / actor_id
→ embedding_model.embed(content, "add")
→ _create_memory(...)
→ vector_store.insert(...)
→ Mem0 history.add_history(...)
```

该路径：

- 不调用 LLM；
- 不进行事实抽取；
- 不执行 Mem0 自身的 ADD/UPDATE/DELETE 决策；
- 每个非 `system` message 生成一条独立 memory；
- 自动生成随机 UUID；
- 自动增加 `data`、`hash`、`created_at`、`updated_at` 和 `text_lemmatized`；
- 返回 `{"results": [{"id", "memory", "event", ...}]}`。

因此 EAGLE Worker 最稳妥的调用方式是每次只传一个字符串或一条 user message，确保一个治理对象对应一个 Mem0 memory：

```python
result = memory.add(
    knowledge.retrieval_text,
    user_id=knowledge.user_id,
    metadata={
        "memory_kind": "K",
        "eagle_memory_id": knowledge.id,
        "knowledge_type": knowledge.knowledge_type,
        "eagle_status": "ACTIVE",
        "version": knowledge.version,
        "index_key": job.index_key,
    },
    infer=False,
)

mem0_id = result["results"][0]["id"]
```

注意：`user_id` 必须使用顶层参数传入。当前代码会从 caller metadata 中移除 `user_id/agent_id/run_id/actor_id`，以防身份域被 metadata 注入。

---

## 19. `infer=True` 为什么必须被阻断

当前 `infer=True` 路径会：

```text
查询相似旧记忆
→ 构造 extraction prompt
→ 调用 LLM
→ 解析新事实
→ embedding
→ 去重
→ 写向量和 history
→ 抽取/关联实体
```

若 EAGLE 已做 Attribution 和 Commitment，再让 Mem0 做一次推断，将形成两个治理中心：

```text
EAGLE Gate
+ Mem0 inference
```

所以正式不变量是：

```text
EAGLE COMMITTED
→ Memory.add(..., infer=False)
```

NoopLLM 的职责是让误用 `infer=True` 快速失败，而不是提供任何生成能力。

---

## 20. 当前 `search` 路径

当前同步 `Memory.search` 签名是：

```python
memory.search(
    query,
    top_k=20,
    filters={...},
    threshold=0.1,
    rerank=False,
    explain=False,
)
```

必须至少包含一个 entity scope：

```python
filters={"user_id": user_id}
```

不能使用旧式调用：

```python
memory.search(query, user_id=user_id)
```

当前内部流程为：

```text
query lemmatization
→ entity extraction
→ query embedding
→ vector_store.search(top_k=max(requested*4, 60))
→ vector_store.keyword_search(...), 可选
→ entity-store boost, 可选
→ score_and_rank
→ threshold
→ format results
```

Kylin Provider 若不实现 `keyword_search`，可继承基类返回 `None`，Mem0 会退化为语义检索并在初始化时输出 warning。这是功能降级，不是安全降级。

EAGLE 的正确调用示例：

```python
response = memory.search(
    query=user_request,
    filters={
        "user_id": user_id,
        "memory_kind": "K",
    },
    top_k=top_k * 3,
)
```

随后必须用返回结果 metadata 中的 `eagle_memory_id` 回查 SQLite，并与当前 `ACTIVE`、Scene、Visibility 和 Environment 条件取交集。

---

## 21. Embedder 扩展点

`EmbeddingBase` 要求：

```python
def embed(
    self,
    text,
    memory_action: Literal["add", "search", "update"] | None,
) -> list[float]:
    ...
```

`embed_batch` 有顺序调用 `embed` 的默认实现；如果麒麟 SDK 支持批量接口，应覆盖它并严格保证输出数量和输入数量一致。

当前 `EmbedderFactory` 的事实约束：

- class path 位于 `EmbedderFactory.provider_to_class`；
- Factory 对所有 provider 统一构造 `BaseEmbedderConfig(**config)`；
- `EmbedderConfig` 另有 provider 白名单；
- `BaseEmbedderConfig` 不是 Pydantic model，而是固定参数的普通类。

因此，如果麒麟 SDK 只需要 `model/api_key/embedding_dims`，可以直接复用 `BaseEmbedderConfig`。如果还需要 endpoint、tenant、设备句柄或注入 client，不能只添加 `kylin.py`，否则额外配置会在 `BaseEmbedderConfig(**config)` 处失败。

推荐为麒麟增加专用配置类，并让 `EmbedderFactory` 支持该 provider 的 config class；现有 provider 继续使用 `BaseEmbedderConfig`，避免大范围重构。

---

## 22. VectorStore 扩展点

`VectorStoreBase` 的必需方法是：

```text
create_col
insert
search
delete
update
get
list_cols
delete_col
col_info
list
reset
```

可选能力是：

```text
keyword_search
search_batch
```

搜索结果对象至少要提供：

```text
id
score
payload
```

其中 score 必须“越大越相似”。基类给出的规范是：

```python
if metric == "cosine_distance":
    score = max(0.0, 1.0 - distance)
elif metric == "l2_distance":
    score = 1.0 / (1.0 + distance)
elif metric == "inner_product":
    score = raw_value
```

EAGLE 当前需要麒麟至少可靠支持：

- 批量 insert/upsert；
- vector search；
- 按 ID get/delete；
- metadata payload 原样保存；
- `user_id` 精确过滤；
- `memory_kind` 精确过滤；
- list + filter，用于 `get_all`、幂等恢复与 reconciliation；
- collection create/info/delete/reset；
- 1024 维或最终选定维度的严格一致性。

---

## 23. Provider 注册的实际修改点

在当前 commit 中，仅新增模块不会自动生效。需要修改：

| 文件 | 修改 |
|---|---|
| `mem0/embeddings/kylin.py` | 新增 `KylinEmbedding` |
| `mem0/configs/embeddings/kylin.py` | 新增专用配置（若 SDK 参数超出 Base config） |
| `mem0/embeddings/configs.py` | 将 `kylin` 加入校验白名单 |
| `mem0/vector_stores/kylin.py` | 新增 `KylinVectorStore` |
| `mem0/configs/vector_stores/kylin.py` | 新增 `KylinVectorStoreConfig` |
| `mem0/vector_stores/configs.py` | 将 `kylin` 映射到配置类 |
| `mem0/llms/noop.py` | 新增 fail-fast `NoopLLM` |
| `mem0/llms/configs.py` | 将 `noop` 加入校验白名单 |
| `mem0/utils/factory.py` | 注册 Kylin Embedder、Kylin VectorStore、NoopLLM |
| `pyproject.toml` | 如麒麟 SDK 可通过包管理安装，则只加入 optional dependency group |

当前各分类的 `__init__.py` 都为空，运行时注册实际发生在 `configs.py` 与 `utils/factory.py`。因此本 commit 不需要为了注册而修改这些 `__init__.py`。

### 23.1 Embedding 配置与 Factory 的最小改法

当前 Factory 对所有 Embedder 强制使用 `BaseEmbedderConfig`。为了支持麒麟专用 client/endpoint，同时不重构现有 provider，增加一个仅含麒麟项的配置映射：

```python
class EmbedderFactory:
    provider_to_class = {
        # existing providers unchanged
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

同时在 `EmbedderConfig.validate_config` 的白名单中增加 `"kylin"`。不要为这一个 provider 改写所有现有 Embedder 配置。

`KylinEmbeddingConfig` 应使用 Pydantic v2，并对注入 client 使用 `ConfigDict(arbitrary_types_allowed=True)`。最终字段只按真实 SDK 增加。

### 23.2 VectorStore 配置与 Factory 的最小改法

VectorStore 已经采用 provider → config class 的动态模式，只需：

```python
class VectorStoreConfig(BaseModel):
    _provider_configs = {
        # existing providers unchanged
        "kylin": "KylinVectorStoreConfig",
    }
```

并在 Factory 注册：

```python
class VectorStoreFactory:
    provider_to_class = {
        # existing providers unchanged
        "kylin": "mem0.vector_stores.kylin.KylinVectorStore",
    }
```

`VectorStoreConfig` 会动态导入 `mem0.configs.vector_stores.kylin`，构造 Pydantic config；Factory 再通过 `model_dump()` 将其展开为 `KylinVectorStore(**config)`。

### 23.3 NoopLLM 配置与 Factory 的最小改法

在 `LlmConfig` 白名单中增加 `"noop"`，并注册：

```python
class LlmFactory:
    provider_to_class = {
        # existing providers unchanged
        "noop": ("mem0.llms.noop.NoopLLM", BaseLlmConfig),
    }
```

配置使用 `{"model": "noop"}`，避免依赖任何真实 LLM 凭据。

### 23.4 不修改 `main.py` 的验证方式

Provider 集成测试应直接用 `Memory.from_config`，证明配置对象、Factory 与运行时类确实连通。只测试 `KylinEmbedding` 或 `KylinVectorStore` 构造成功，不足以证明 `Memory.from_config` 可以使用 `provider="kylin"`。

---

## 24. Mem0 自带 SQLite 的边界

`mem0/memory/storage.py` 中的 `SQLiteManager` 只管理：

```text
history
messages
```

它记录 Mem0 memory 的 ADD/UPDATE/DELETE 历史与最近消息，不包含：

- Candidate；
- Evidence；
- Preference hardness；
- Knowledge condition；
- P-P/K-K/P-K conflict；
- Environment validation；
- Outbox；
- EAGLE lifecycle。

因此它不能替代 EAGLE SQLite。推荐使用两个独立文件：

```text
eagle.db          # 权威治理状态，Alembic/SQLAlchemy 管理
mem0_history.db   # Mem0 内部 history/messages
```

两者分离可避免 EAGLE migration 与 Mem0 内部 schema 演化互相影响。

### 24.1 当前基线的兼容性风险矩阵

| 风险 | 根因 | 后果 | 第一阶段处理 |
|---|---|---|---|
| 错用顶层 `user_id` 搜索 | 当前 `search` 只接受 `filters` | 直接抛 `ValueError` | Gateway 固定当前签名并做集成测试 |
| NoopLLM 未配置 | `Memory.__init__` 总会构造 LLM | 启动依赖默认 LLM/凭据 | 显式配置并断言 `noop` |
| Vector filter 语义不一致 | Base 接口接收 dict，但各 provider 翻译能力不同 | 漏召回或隔离失败 | 启动 capability probe；tenant EQ 不通过则失败 |
| score 方向错误 | SDK 可能返回 distance | 阈值和排序完全反向 | contract test 固定“越大越相似” |
| embedding 维度不一致 | `Memory` 初始化不统一校验两个 provider 的维度 | 首次 insert/search 才失败 | 启动 smoke insert/search 并断言维度 |
| Outbox 重试产生重复向量 | raw add 内部生成随机 UUID | 物理重复 | `index_key` + reconciliation + PACK 去重 |
| Mem0 history 与向量不一致 | `_create_memory` 先 vector insert 后 history insert | add 抛错但向量残留 | 不依赖 Mem0 history；按 `index_key` 收敛 |
| 搜索附加创建 entity collection | entity extraction 命中时懒加载 entity store | 麒麟出现额外 collection/调用 | Provider 支持派生 collection；测试或关闭相应运行条件 |
| telemetry 创建额外 collection | `MEM0_TELEMETRY` 默认开启 | 非业务索引与额外成本 | EAGLE 进程启动前显式关闭，或完整支持 |
| Mode C 过滤后结果不足 | 向量先召回、SQLite 后过滤 | Knowledge Recall@K 降低 | oversampling、监控过滤率、优先推进原生 filter |
| Async 吞吐受线程池影响 | `AsyncMemory` 用 `asyncio.to_thread` 包同步 provider | 高并发阻塞/线程争用 | SDK client 必须线程安全并做并发基准 |
| injected client 无法 deepcopy | entity/telemetry config 会被复制 | 派生 store 初始化失败 | 优先注入可复制的 client factory；用集成测试验证 |

这张表中的安全项和质量项必须分开：keyword search 不可用可以降低检索质量；tenant filter 不可靠则属于隔离失败，必须阻止启动。

---

# 第三部分：代码架构与修改方案

## 25. 推荐仓库布局

直接以当前 Mem0 fork 为代码基座，不复制 `Memory`：

```text
mem0/                                  # 当前仓库根目录
├── mem0/                              # Mem0 Python SDK
│   ├── memory/main.py                 # 不改
│   ├── embeddings/kylin.py            # 新增
│   ├── vector_stores/kylin.py         # 新增
│   ├── llms/noop.py                   # 新增
│   ├── configs/embeddings/kylin.py    # 新增
│   ├── configs/vector_stores/kylin.py # 新增
│   └── utils/factory.py               # 注册 provider
│
├── eagle_os_agent/                    # 独立应用子项目
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── eagle/
│   │   ├── domain/
│   │   ├── db/
│   │   ├── episode/
│   │   ├── attribution/
│   │   ├── gate/
│   │   ├── preference/
│   │   ├── knowledge/
│   │   ├── pack/
│   │   ├── conflict/
│   │   ├── forgetting/
│   │   ├── materialization/
│   │   └── outbox/
│   ├── adapters/
│   │   ├── mem0_gateway.py
│   │   ├── kylin/
│   │   │   ├── embedding_client.py
│   │   │   └── vector_client.py
│   │   └── os_tools/executor.py
│   ├── api/
│   └── tests/
│       ├── unit/
│       ├── integration/
│       ├── scenarios/
│       └── benchmark/
│
└── EAGLE_MEM0_DESIGN.md
```

使用子项目的原因是当前根 `pyproject.toml` 的 wheel 明确只打包 `mem0`。直接在根新增 `eagle/` 而不修改打包配置，会导致源码在仓库中可见、安装后却不存在。独立 `eagle_os_agent/pyproject.toml` 可以让 EAGLE 应用依赖当前锁定的 Mem0 fork，同时保持两个发布单元的边界。

如果比赛交付明确要求单 wheel，再把根 Hatch 配置扩展为同时打包两个 package；在此要求出现之前，不先扩大 Mem0 的发布边界。

---

## 26. 依赖方向

```text
API / OS Agent Runtime
        ↓
EAGLE Application Services
        ↓
Domain + Repositories + Ports
        ↓
Adapters
   ├── Mem0 Gateway
   ├── Kylin SDK Client
   └── OS Tool Executor
        ↓
Mem0 Provider Interfaces
        ↓
Kylin SDK / Vector Engine
```

约束：

- `eagle/domain` 不导入 Mem0 或麒麟 SDK；
- Attribution、Gate、Resolver 和 Compiler 可纯单元测试；
- 只有 `adapters/mem0_gateway.py` 了解 Mem0 返回结构；
- Mem0 Provider 不了解 Candidate、Preference 或 Gate；
- Kylin SDK 的真实方法名只出现在 `adapters/kylin/*` 或 Provider 最薄边界中。

---

## 27. EAGLE 数据模型

### 27.1 `episodes`

```text
id TEXT PK
user_id TEXT NOT NULL
session_id TEXT NOT NULL
scene_json JSON NOT NULL
request_text TEXT NOT NULL
tool_name TEXT NOT NULL
arguments_digest TEXT NOT NULL
success BOOLEAN NOT NULL
result_class TEXT NULL
error_code TEXT NULL
latency_ms INTEGER NOT NULL
retry_count INTEGER NOT NULL DEFAULT 0
fallback_from TEXT NULL
previous_error_code TEXT NULL
user_intervention BOOLEAN NOT NULL DEFAULT false
user_correction JSON NULL
environment_fingerprint TEXT NOT NULL
created_at DATETIME NOT NULL
```

不默认保存完整工具参数或工具结果，避免将敏感数据无边界地复制到长期数据库。只有 Attribution 必需且符合隐私策略的字段才进入结构化列。

### 27.2 `evidence`

```text
id TEXT PK
episode_id TEXT NOT NULL FK episodes(id)
candidate_id TEXT NOT NULL FK candidates(id)
evidence_type TEXT NOT NULL
direction TEXT NOT NULL
strength TEXT NOT NULL
contribution INTEGER NOT NULL
confidence_delta REAL NOT NULL
attribution_reason TEXT NOT NULL
condition_json JSON NOT NULL
created_at DATETIME NOT NULL

UNIQUE(candidate_id, episode_id, evidence_type, direction, attribution_reason)
```

`attribution_reason` 必须参与唯一键：同一 Episode 可以同时产生方向相同、但语义不同的显式强证据和隐式弱证据；四列唯一键会错误吞掉其中一条（实现方案 §16.3）。

独立 Evidence 表比只保存累计计数更适合 I7 Traceability，也能避免 Episode 重放时重复计数。

### 27.3 `candidates`

```text
id TEXT PK
candidate_identity TEXT NOT NULL UNIQUE
user_id TEXT NOT NULL
candidate_type TEXT NOT NULL              # P | K
candidate_key TEXT NOT NULL
candidate_value_json JSON NOT NULL
scene_json JSON NOT NULL
positive_evidence INTEGER NOT NULL DEFAULT 0
negative_evidence INTEGER NOT NULL DEFAULT 0
independent_choices INTEGER NOT NULL DEFAULT 0
distinct_sessions INTEGER NOT NULL DEFAULT 0
same_condition_success INTEGER NOT NULL DEFAULT 0
confidence REAL NOT NULL DEFAULT 0
explicit_user_statement BOOLEAN NOT NULL DEFAULT false
user_confirmed BOOLEAN NOT NULL DEFAULT false
has_unresolved_conflict BOOLEAN NOT NULL DEFAULT false
state TEXT NOT NULL                       # PENDING | COMMITTED | REJECTED | DEFER
created_at DATETIME NOT NULL
updated_at DATETIME NOT NULL
```

累计列用于 Gate 快速决策，但其值必须在插入新 Evidence 的同一事务内更新。`distinct_sessions` 必须按 Evidence 关联 Episode 的 distinct session 计算或受唯一辅助表约束，不能简单每次 `+1`。

### 27.4 `preferences`

```text
id TEXT PK
user_id TEXT NOT NULL
preference_key TEXT NOT NULL
preference_value_json JSON NOT NULL
hardness TEXT NOT NULL                    # HARD | SOFT
scene_json JSON NOT NULL
confidence REAL NOT NULL
version INTEGER NOT NULL
parent_version_id TEXT NULL FK preferences(id)
status TEXT NOT NULL                      # ACTIVE | MASKED | REVOKED | FORGETTING | FORGOTTEN
authorization_state TEXT NOT NULL
mem0_id TEXT NULL
created_at DATETIME NOT NULL
updated_at DATETIME NOT NULL
```

### 27.5 `knowledge`

```text
id TEXT PK
user_id TEXT NOT NULL
knowledge_type TEXT NOT NULL               # WORKFLOW | CASE | TEMPLATE | FACT
retrieval_text TEXT NOT NULL
content_json JSON NOT NULL
scene_json JSON NOT NULL
environment_fingerprint TEXT NOT NULL
confidence REAL NOT NULL
version INTEGER NOT NULL
parent_version_id TEXT NULL FK knowledge(id)
status TEXT NOT NULL                       # ACTIVE | NEEDS_REVALIDATION | REVOKED | FORGETTING | FORGOTTEN
mem0_id TEXT NULL
created_at DATETIME NOT NULL
updated_at DATETIME NOT NULL
```

### 27.6 `memory_evidence_links`

```text
id TEXT PK
memory_kind TEXT NOT NULL                  # P | K
memory_id TEXT NOT NULL
evidence_id TEXT NOT NULL FK evidence(id)
created_at DATETIME NOT NULL

UNIQUE(memory_kind, memory_id, evidence_id)
```

提交 Candidate 时，至少复制一条关联，数据库约束和 service 共同维护 I7。

### 27.7 `pk_visibility`

```text
id TEXT PK
preference_version_id TEXT NOT NULL FK preferences(id)
knowledge_version_id TEXT NOT NULL FK knowledge(id)
reason TEXT NOT NULL
active BOOLEAN NOT NULL
created_at DATETIME NOT NULL
updated_at DATETIME NOT NULL

UNIQUE(preference_version_id, knowledge_version_id)
```

### 27.8 `index_jobs`

```text
id TEXT PK
index_key TEXT NOT NULL UNIQUE
memory_kind TEXT NOT NULL                  # P | K
memory_id TEXT NOT NULL
operation TEXT NOT NULL                    # UPSERT | DELETE
state TEXT NOT NULL                        # PENDING | RUNNING | DONE | FAILED
retry_count INTEGER NOT NULL DEFAULT 0
last_error TEXT NULL
available_at DATETIME NOT NULL
created_at DATETIME NOT NULL
updated_at DATETIME NOT NULL
```

`index_key` 建议为：

```text
{operation}:{memory_kind}:{memory_id}:{version}
```

它提供 EAGLE 事务内的 job 去重键，也作为 Mem0 metadata 的恢复键。

---

## 28. Episode Collector 与统一工具执行入口

所有工具执行必须经过唯一 executor：

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
        latency_ms = int((time.perf_counter() - started) * 1000)
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
                latency_ms=latency_ms,
                environment_fingerprint=context.environment_fingerprint,
                fallback_from=context.fallback_from,
                previous_error_code=context.previous_error_code,
            )
        )
```

若 Episode 落库失败，不应伪装成工具执行失败。产品需明确执行成功但证据记录失败时的策略；第一阶段建议让 collector 错误显式冒泡到 orchestration 层并记录运行失败，而不是静默吞掉。

Fallback 的第二条 Episode 必须包含 `fallback_from` 与 `previous_error_code`，否则无法区分“用户主动选择 B”和“A 失败后被迫使用 B”。

---

## 29. Attribution 接口与规则

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
def attribute_fallback(episode: Episode) -> AttributionResult | None:
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

第一阶段显式 Preference 识别可以基于结构化 NLU 事件。中文触发词如“以后、今后、默认、每次都、始终、不要再、绝不”只应作为候选信号，最终输出必须包含 key、value、hardness 和 Scene，不能直接把原句当作 HARD 约束执行。

---

## 30. Candidate 聚合与 Gate 事务

处理单个 Episode 的事务：

```text
BEGIN IMMEDIATE

INSERT Episode

FOR each AttributionResult:
    compute candidate_identity
    INSERT Candidate OR load existing Candidate
    INSERT Evidence with unique replay key
    if Evidence was newly inserted:
        update aggregate counters
    decision = Gate(candidate)

    if decision == COMMITTED:
        Candidate.state = COMMITTED
        INSERT Preference or Knowledge
        INSERT MemoryEvidenceLink(s)
        INSERT IndexJob(UPSERT)

COMMIT
```

对同一 Candidate 的并发更新应通过 SQLite 写事务串行化，并依赖唯一索引防止重复 Candidate 和重复 Evidence。不要先查后插而没有唯一约束。

Gate 第一版：

```python
def decide_preference(candidate: Candidate) -> CandidateState:
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


def decide_knowledge(candidate: Candidate) -> CandidateState:
    if candidate.user_confirmed:
        return CandidateState.COMMITTED
    if candidate.same_condition_success >= 2:
        return CandidateState.COMMITTED
    return CandidateState.PENDING
```

---

## 31. Preference Resolver 与 Constraint Compiler

### 31.1 Scene Match

一条 Preference 匹配当前 Scene，当且仅当该 Preference Scene 中所有非空字段都与当前 Scene 相等。

同 key 匹配项按以下顺序决议：

```text
specificity DESC
→ version DESC
→ created_at DESC
```

若 specificity 和 version 均无法消除冲突，不猜测，返回 conflict 并阻止相关动作。

### 31.2 统一约束对象

```python
@dataclass(frozen=True)
class PlannerConstraint:
    allowed_tools: frozenset[str] | None
    denied_tools: frozenset[str]
    require_offline: bool
    allowed_formats: frozenset[str] | None
    privacy_rules: tuple[str, ...]
```

### 31.3 物理过滤

```python
def apply_constraints(all_tools, constraints: PlannerConstraint):
    tools = [tool for tool in all_tools if tool.name not in constraints.denied_tools]

    if constraints.allowed_tools is not None:
        tools = [tool for tool in tools if tool.name in constraints.allowed_tools]

    if constraints.require_offline:
        tools = [tool for tool in tools if not tool.requires_network]

    return tools
```

若过滤后 action space 为空，应返回明确的 `NO_FEASIBLE_ACTION`，而不是绕过 HARD Preference。

---

## 32. Mem0 Gateway

EAGLE 只通过一个薄 Gateway 调用 Mem0，集中固定 `infer=False`、metadata schema 和结果解析：

```python
class Mem0Gateway:
    def __init__(self, memory: Memory):
        self._memory = memory

    def index_knowledge(self, knowledge: Knowledge, index_key: str) -> str:
        result = self._memory.add(
            knowledge.retrieval_text,
            user_id=knowledge.user_id,
            metadata={
                "memory_kind": "K",
                "eagle_memory_id": knowledge.id,
                "knowledge_type": knowledge.knowledge_type.value,
                "eagle_status": "ACTIVE",
                "version": knowledge.version,
                "index_key": index_key,
            },
            infer=False,
        )
        return result["results"][0]["id"]

    def search_knowledge(self, query: str, user_id: str, top_k: int):
        return self._memory.search(
            query=query,
            filters={
                "user_id": user_id,
                "memory_kind": "K",
            },
            top_k=top_k,
        )["results"]

    def delete(self, mem0_id: str) -> None:
        self._memory.delete(memory_id=mem0_id)
```

Gateway 不决定 lifecycle，也不把 Mem0 metadata 当作权威状态。

启动时必须断言：

```python
assert memory.config.embedder.provider == "kylin"
assert memory.config.vector_store.provider == "kylin"
assert memory.config.llm.provider == "noop"
```

配置失败直接终止启动，不允许 fallback 到默认 OpenAI/Qdrant。

---

## 33. Outbox Worker、幂等性与一致性

### 33.1 基本 Worker

```text
claim PENDING/FAILED job
→ state = RUNNING
→ call Mem0
→ update memory.mem0_id
→ state = DONE
```

外部调用不能放进长期 SQLite 事务中。Worker claim 使用短事务；网络调用结束后再用短事务提交结果。

### 33.2 重要限制：`Memory.add` 不接受调用方指定 ID

当前 Mem0 raw insert 内部生成随机 UUID。因此存在如下崩溃窗口：

```text
Mem0.add 成功
→ 进程崩溃
→ EAGLE 尚未写回 mem0_id
→ job 重试
→ 产生第二条向量
```

所以“Outbox = exactly once”是不成立的。第一阶段应明确采用：

```text
at-least-once delivery
+ convergent reconciliation
```

收敛策略：

1. 每次写入 metadata 带唯一 `index_key` 和 `eagle_memory_id`；
2. Worker 重试 UPSERT 前先用 `get_all(filters={user_id, index_key})` 查找既有条目；
3. 若恰有一条，则回填其 Mem0 ID，不重复 add；
4. 若有多条，保留一条并为其余条目创建 DELETE job；
5. PACK 按 `eagle_memory_id` 去重，并始终回查 SQLite；
6. 若麒麟索引是最终一致的，仍可能短暂重复，但不会让未授权 Memory 生效。

如果后续必须证明物理 exactly-once，需要对 Mem0 增加“调用方指定 memory_id”的公共能力；这会修改 `memory/main.py` 和公共 API，不属于当前“不侵入主流程”的第一阶段边界。

### 33.3 Mem0 内部 history 失败窗口

当前 `_create_memory` 先 `vector_store.insert`，再 `db.add_history`。history 写失败时，向量可能已存在而 `Memory.add` 抛错。相同的 `index_key` reconciliation 可以恢复该情况。因此 EAGLE 不应依赖 Mem0 history 判断索引是否成功。

---

## 34. PACK 的确定性安全过滤

```python
def pack(request, scene, environment, user_id, top_k):
    active_preferences = preference_repository.list_active(user_id)
    resolved = preference_resolver.resolve(active_preferences, scene)
    constraints = constraint_compiler.compile(resolved.hard)

    raw_results = mem0_gateway.search_knowledge(
        query=request,
        user_id=user_id,
        top_k=top_k * 3,
    )

    ids = {
        item["metadata"]["eagle_memory_id"]
        for item in raw_results
        if item.get("metadata", {}).get("eagle_memory_id")
    }
    authoritative = knowledge_repository.list_active_by_ids(user_id, ids)

    visible = visibility_filter.apply(authoritative, resolved)
    valid = environment_filter.apply(visible, environment)
    ranked = soft_preference_ranker.rank(valid, resolved.soft)

    return PlannerContext(
        constraints=constraints,
        knowledge=ranked[:top_k],
    )
```

关键性质：

- `PENDING` 从未写入 Mem0；
- stale vector metadata 不能绕过 SQLite；
- `FORGETTING` 在 Worker 删除前已经不可见；
- HARD 工具过滤与向量 filter 能力无关；
- 缺失 `eagle_memory_id` 的向量结果直接丢弃，不猜测映射。

---

## 35. Kylin Filter Capability Probe

启动时在隔离的 probe collection 写入少量测试数据，实际验证：

```text
EQ
IN
NE / NOT
AND
OR（如计划使用）
vector ID allowlist
vector ID blocklist
list + metadata filter
delete visibility latency
read-after-write consistency
```

能力模式：

### Mode A：PRE_FILTER

麒麟原生支持需要的 metadata filter，Mem0 直接传入过滤表达式。

### Mode B：ELIGIBLE-ID FILTER

SQLite 先得到 eligible Knowledge IDs，再通过 `knowledge_id IN (...)` 或原生 vector-ID allowlist 限制搜索。只有 capability probe 证明对应能力可用时才能启用。

### Mode C：OVERSAMPLE + POST-FILTER

扩大 Mem0 `top_k`，召回后与 SQLite eligible set 取交集。该模式可能降低 Knowledge Recall，但不得降低 tenant isolation 或 HARD Constraint 安全性。

最低启动门槛：

```text
user_id EQ filter 正确
AND read-after-write/delete 行为已知
```

若麒麟无法可靠隔离 `user_id`，系统应启动失败，不能用跨租户 oversampling 后过滤作为常规方案。

---

## 36. KylinEmbedding 实现骨架

麒麟 SDK 的包名、构造参数和返回结构尚未在当前仓库中提供，以下只定义 Mem0 侧契约，不伪造 SDK 方法名：

```python
from typing import Literal

from mem0.embeddings.base import EmbeddingBase


class KylinEmbedding(EmbeddingBase):
    def __init__(self, config):
        super().__init__(config)
        self._client = config.client
        self._dimension = config.embedding_dims

    def embed(
        self,
        text: str,
        memory_action: Literal["add", "search", "update"] | None = None,
    ) -> list[float]:
        vector = self._client.embed(text=text, action=memory_action)
        if len(vector) != self._dimension:
            raise RuntimeError(
                f"Kylin embedding dimension mismatch: "
                f"expected {self._dimension}, got {len(vector)}"
            )
        return [float(value) for value in vector]

    def embed_batch(self, texts, memory_action="add"):
        vectors = self._client.embed_batch(texts=texts, action=memory_action)
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"Kylin embed_batch returned {len(vectors)} vectors "
                f"for {len(texts)} texts"
            )
        for vector in vectors:
            if len(vector) != self._dimension:
                raise RuntimeError("Kylin embedding dimension mismatch")
        return [[float(value) for value in vector] for vector in vectors]
```

边界校验只做 SDK 返回数量、数值类型和维度，因为这是外部系统边界。SDK 异常应保留 cause 并向上抛出，不返回空向量或静默切换其他 Embedder。

当前 `AsyncMemory` 会通过 `asyncio.to_thread` 调用同步 Embedder，因此第一版麒麟 client 应提供同步且线程安全的接口；不要在同步 `embed` 内嵌套运行 event loop。

---

## 37. KylinVectorStore 实现骨架

```python
from typing import Any

from pydantic import BaseModel

from mem0.vector_stores.base import VectorStoreBase


class OutputData(BaseModel):
    id: str
    score: float | None = None
    payload: dict[str, Any] | None = None


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
        self.create_col(collection_name, embedding_model_dims, distance_metric)

    def create_col(self, name, vector_size, distance):
        ...

    def insert(self, vectors, payloads=None, ids=None):
        ...

    def search(self, query, vectors, top_k=5, filters=None):
        rows = ...
        return [
            OutputData(
                id=str(row.id),
                score=self._to_similarity(row),
                payload=row.payload,
            )
            for row in rows
        ]

    def delete(self, vector_id):
        ...

    def update(self, vector_id, vector=None, payload=None):
        ...

    def get(self, vector_id):
        ...

    def list_cols(self):
        ...

    def delete_col(self):
        ...

    def col_info(self):
        ...

    def list(self, filters=None, top_k=None):
        ...

    def reset(self):
        self.delete_col()
        self.create_col(
            self.collection_name,
            self.embedding_model_dims,
            self.distance_metric,
        )
```

`update(vector=None, payload=...)` 必须保留旧 vector，因为 Mem0 的 metadata-only 更新允许 vector 为 `None`。如果麒麟 SDK 没有 metadata-only update，就先 get 原 vector 再 upsert；如果 get API 无法返回 vector，则 SDK 能力不足，需要在实现前明确解决。

`list` 建议返回 flat `list[OutputData]`；当前 Mem0 同时兼容 flat list 和一层嵌套 list，但新 Provider 不应延续历史不一致格式。

---

## 38. NoopLLM

```python
from mem0.llms.base import LLMBase


class NoopLLM(LLMBase):
    def generate_response(self, messages, tools=None, tool_choice="auto", **kwargs):
        raise RuntimeError(
            "NoopLLM was invoked. EAGLE persistence must call Memory.add with infer=False."
        )
```

使用 `BaseLlmConfig(model="noop")` 即可满足当前 `LLMBase` 对 `model` 属性的校验。NoopLLM 不返回空字符串，因为那会把配置错误伪装成“没有抽取到 Memory”。

---

## 39. 正式配置

以下配置中的 `client` 仅表示注入后的适配器对象；最终字段要按真实麒麟 SDK 和配置安全策略调整：

```python
config = {
    "embedder": {
        "provider": "kylin",
        "config": {
            "model": "<confirmed-kylin-model>",
            "embedding_dims": 1024,
            "client": kylin_embedding_client,
        },
    },
    "vector_store": {
        "provider": "kylin",
        "config": {
            "collection_name": "eagle_memories",
            "embedding_model_dims": 1024,
            "distance_metric": "<confirmed-metric>",
            "client": kylin_vector_client,
        },
    },
    "llm": {
        "provider": "noop",
        "config": {"model": "noop"},
    },
    "history_db_path": "/var/lib/eagle/mem0_history.db",
}

memory = Memory.from_config(config)
```

必须先确认麒麟返回的是 similarity 还是 distance，以及 cosine/L2/inner-product 的具体定义。未确认前不能编写 `_to_similarity` 的最终代码。

---

## 40. Forgetting 与 Reconciliation

### 40.1 Forget 事务

```text
BEGIN
assert current status == ACTIVE
status = FORGETTING
insert IndexJob(DELETE)
deactivate related visibility rows
COMMIT
```

提交后 PACK 立即不可见。

### 40.2 Delete Worker

```text
if mem0_id is NULL:
    reconcile by index_key/eagle_memory_id

if vector exists:
    Memory.delete(mem0_id)

BEGIN
mem0_id = NULL
status = FORGOTTEN
job.state = DONE
COMMIT
```

Mem0 `delete` 会先 get，找不到时抛 `ValueError`。Worker 只有在已通过精确 reconciliation 证明向量不存在时，才把“not found”视作收敛成功；不能宽泛捕获所有 `ValueError`。

### 40.3 启动 Reconciliation

```text
ACTIVE + mem0_id NULL
→ ensure UPSERT job

ACTIVE + mem0_id present but vector missing
→ ensure UPSERT job

FORGETTING + mem0_id present
→ ensure DELETE job

FORGOTTEN + mem0_id present
→ ensure DELETE job and raise health warning

FAILED job whose available_at elapsed
→ retry

duplicate vectors with same index_key
→ retain canonical one, enqueue deletion for the rest
```

Reconciliation 证明派生索引可从权威状态收敛重建，但不把 Mem0 history 当作权威输入。

---

## 41. API 最小集合

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

`/health` 至少区分：

```text
database_ready
mem0_ready
kylin_embedding_ready
kylin_vector_ready
outbox_backlog
failed_jobs
reconciliation_required
```

`/capabilities` 输出 probe 的真实结果和当前检索模式，不根据配置文件声称支持某能力。

---

## 42. 测试方案

### 42.1 EAGLE 核心不变量测试

1. `WPS fail → LibreOffice success`：产生 K Candidate，不产生 P Candidate。
2. 同一隐式选择两次：保持 `PENDING`。
3. 第三次且跨两个 session：进入 `COMMITTED`。
4. “以后编辑 DOCX 都用 WPS”：一次提交为 HARD Preference。
5. HARD `preferred_tool=WPS`：Planner action space 只含 WPS。
6. `FORGETTING K`：即使向量仍存在，PACK 不返回。
7. `PENDING`：不得存在 Preference/Knowledge 行，也不得产生 IndexJob。
8. 每条 COMMITTED Memory：至少一个 Evidence Link。
9. 环境 fingerprint 不匹配：K 不进入 PlannerContext，并进入重验证流程。
10. P-K conflict：K 保留但 visibility 为 false；P 撤销后恢复。

### 42.2 Mem0 Provider 单元测试

`tests/embeddings/test_kylin_embeddings.py`：

- 单条 embedding 映射；
- batch 顺序与数量；
- 维度不匹配快速失败；
- SDK 异常不被吞掉；
- `memory_action` 正确传递或按已确认契约映射。

`tests/vector_stores/test_kylin.py`：

- 初始化/collection 创建；
- insert payload 和 ID；
- search filter 翻译；
- score 方向与范围；
- get/delete；
- metadata-only update；
- list/filter；
- reset；
- 不支持的 filter operator 明确失败。

`tests/llms/test_noop.py`：

- Factory 可创建；
- `generate_response` 总是抛出明确错误。

### 42.3 Mem0 集成测试

- `Memory.from_config` 确认三个 provider 类型；
- `Memory.add(..., infer=False)` 只调用一次麒麟 embedding；
- raw insert 返回一条 ID；
- `Memory.search(filters={"user_id": ...})` 不跨 user；
- `Memory.delete` 后 get/search 不可见；
- 误用 `infer=True` 被 NoopLLM 阻断；
- metadata 中的 `eagle_memory_id/index_key` 可用于 reconciliation。

### 42.4 故障注入

- SQLite commit 前进程退出：无正式 Memory、无 IndexJob；
- SQLite commit 后 Worker 前退出：job 可恢复；
- Mem0 add 后、回填 mem0_id 前退出：通过 index_key 收敛；
- delete 失败：保持 `FORGETTING`；
- stale vector 返回：SQLite intersection 丢弃；
- duplicate vector 返回：按 `eagle_memory_id` 去重；
- 麒麟 read-after-write 延迟：能力报告与 retry 行为符合实测。

---

## 43. 分阶段实施与成功标准

### Phase 0：冻结基线

工作：

- 记录 Mem0 commit；
- 保持 fork 与 upstream 的同步策略；
- 为 EAGLE 子项目建立独立依赖锁。

成功标准：构建、现有核心测试和最小 Memory smoke test 在该 commit 可复现。

### Phase 1：Provider 契约测试

工作：

- 先用 fake Kylin clients 写 Provider contract tests；
- 添加 NoopLLM；
- 不接真实 SDK。

成功标准：Factory、配置验证、raw add/search/delete 的测试通过，`main.py` 无修改。

### Phase 2：EAGLE Schema

工作：

- SQLAlchemy 2.x models；
- Alembic migration；
- repository；
- SQLite 约束和唯一索引。

成功标准：schema migration 可从空库创建；Candidate/Evidence replay 不重复计数；COMMITTED 无 Evidence 时事务失败。

### Phase 3：核心创新闭环

工作：

```text
Episode → Attribution → Candidate → Gate
```

成功标准：Fallback Non-Promotion、隐式 Preference 阈值、显式 Preference 和 Knowledge 重复成功测试全部通过。

### Phase 4：Outbox + Mem0

工作：

```text
COMMITTED → SQLite → IndexJob → Worker → Memory.add(infer=False)
```

成功标准：故障注入后最终收敛；PENDING 永不索引；worker 不调用 `infer=True`。

### Phase 5：PACK

工作：

```text
SQLite Preference + Mem0 Knowledge → PlannerContext
```

成功标准：HARD violation 测试为 0；FORGETTING leakage 为 0；stale vector 无法绕过 SQLite。

### Phase 6：真实 Kylin Embedding

工作：

- 根据真实 SDK 实现 adapter；
- 单条/batch/并发/维度 smoke test；
- 明确超时和异常类型。

成功标准：无 fallback，维度严格一致，SDK 异常可观测。

### Phase 7：真实 Kylin VectorStore

工作：

- 实现完整抽象；
- capability probe；
- score normalization；
- tenant filter；
- consistency 测试。

成功标准：Provider contract、Mem0 integration 和 tenant isolation 测试通过。

### Phase 8：治理增强

工作：

- P-P/K-K/P-K；
- revalidation；
- forgetting；
- reconciliation；
- materialization（如确有 OS 配置写入需求）。

成功标准：相关不变量与故障注入测试通过。

### Phase 9：实验

比较：

```text
Mem0 original
vs Mem0 + simple preference extraction
vs Mem0 + EAGLE
```

报告 Memory Precision、False Promotion、HARD violation、Stale Knowledge Reuse、Forget Leakage 与任务成功率，不只报告 Vector Recall。

---

## 44. 文件级改造清单

### 44.1 Mem0 fork：必须修改

```text
mem0/embeddings/kylin.py
mem0/configs/embeddings/kylin.py
mem0/embeddings/configs.py

mem0/vector_stores/kylin.py
mem0/configs/vector_stores/kylin.py
mem0/vector_stores/configs.py

mem0/llms/noop.py
mem0/llms/configs.py

mem0/utils/factory.py

tests/embeddings/test_kylin_embeddings.py
tests/vector_stores/test_kylin.py
tests/llms/test_noop.py
tests/integration/test_eagle_mem0_providers.py
```

### 44.2 Mem0 fork：条件修改

```text
pyproject.toml
```

只有麒麟 SDK 以可安装 Python 包形式提供时，才增加 optional dependency，例如独立的 `kylin` feature；不加入 core dependencies。

### 44.3 Mem0 fork：第一阶段不修改

```text
mem0/memory/main.py
mem0/memory/storage.py
```

### 44.4 EAGLE 子项目：新增

```text
eagle_os_agent/pyproject.toml
eagle_os_agent/alembic.ini
eagle_os_agent/eagle/domain/*
eagle_os_agent/eagle/db/*
eagle_os_agent/eagle/episode/*
eagle_os_agent/eagle/attribution/*
eagle_os_agent/eagle/gate/*
eagle_os_agent/eagle/preference/*
eagle_os_agent/eagle/knowledge/*
eagle_os_agent/eagle/pack/*
eagle_os_agent/eagle/conflict/*
eagle_os_agent/eagle/forgetting/*
eagle_os_agent/eagle/outbox/*
eagle_os_agent/adapters/mem0_gateway.py
eagle_os_agent/adapters/kylin/*
eagle_os_agent/adapters/os_tools/executor.py
eagle_os_agent/api/*
eagle_os_agent/tests/*
```

---

## 45. 已知待确认项

开始真实麒麟实现前，必须取得以下事实：

1. Embedding SDK 的包名、初始化方式、同步/异步接口和 batch 限制；
2. Embedding 模型名称与固定维度；
3. Vector SDK 的 collection/index 生命周期 API；
4. insert 是 insert 还是 upsert；
5. metadata 支持的数据类型和大小限制；
6. filter 表达式以及 EQ/IN/NOT/AND/ID allowlist 能力；
7. 返回值是 similarity 还是 distance，使用何种 metric；
8. get 是否能返回原 vector；
9. metadata-only update 是否可用；
10. list/pagination API；
11. delete 的 read-after-delete 一致性；
12. client 是否线程安全；
13. 凭据加载与密钥轮换方式。

这些信息缺失时，可以完成 EAGLE 核心闭环和 fake-client Provider contract，但不能声称真实 Kylin Provider 已实现或验证。

---

## 46. 最终调用链

```text
用户请求
   │
   ▼
EAGLE PACK
   │
   ├── SQLite ACTIVE Preference
   │      ↓
   │   Scene Resolve
   │      ↓
   │   HARD Constraint Compiler
   │      ↓
   │   Physical Tool Filter
   │
   └── Mem0.search(filters={user_id, memory_kind=K})
          │
          ▼
      KylinEmbedding
          │
          ▼
      KylinVectorStore
          │
          ▼
      Kylin Vector Engine
          │
          ▼
      SQLite ACTIVE Intersection
          │
          ▼
      Visibility + Environment + Soft Ranking
   │
   ▼
Planner
   │
   ▼
Constrained Tool Executor
   │
   ▼
Episode
   │
   ▼
Evidence Attribution
   │
   ▼
Candidate
   │
   ▼
Commitment Gate
   │
   ├── PENDING
   ├── REJECTED
   ├── DEFER
   └── COMMITTED
           │
           ▼
        EAGLE SQLite
        + EvidenceLink
        + IndexJob
           │
           ▼
      Outbox Worker
           │
           ▼
      Memory.add(
        infer=False
      )
```

---

## 47. 最终工程定义

> 不重写 Mem0 Memory；以当前锁定 commit 的 Mem0 OSS Python 作为底层语义记忆代码基座，仅扩展 Kylin Embedding、Kylin VectorStore 和 fail-fast NoopLLM Provider。EAGLE 作为独立治理层实现 Episode、Evidence Attribution、Candidate、Commitment Gate、Preference/Knowledge 权威状态、PACK、冲突、环境重验证、版本与遗忘。只有 `COMMITTED` 记忆才通过 `Memory.add(..., infer=False)` 进入派生语义索引；任何检索结果在进入 Planner 前都必须与 SQLite 当前权威状态重新取交集。

这一边界保持了唯一治理权：

```text
EAGLE 决定“该不该记、能不能用、如何影响决策”
Mem0 决定“如何以统一接口写入、检索和删除语义记忆”
Kylin 决定“如何生成向量并执行向量存储与搜索”
```
