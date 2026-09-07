# EAGLE 方法论

## 面向 OS Agent 的执行证据驱动长期记忆治理方法

> 文档类型：论文式方法章节  
> 实现基线：Mem0 OSS `9a7924befd7026e41e445ba809370009e5e985a6`  
> 日期：2026-09-04

---

## 1. 方法概述

EAGLE 研究的问题不是如何从对话中抽取更多事实，而是如何控制历史经验获得未来决策影响权的过程。

给定一次 OS Agent 执行轨迹，普通长期记忆系统通常直接进行：

```text
Execution History
→ Memory Extraction
→ Vectorization
→ Retrieval
```

这种路径隐含了两个未经验证的等价关系：

$$
ObservedAction \equiv UserPreference
$$

$$
SuccessfulExecution \equiv StableKnowledge
$$

EAGLE 不接受这两个等价关系，而采用：

```text
Execution Episode
→ Evidence Attribution
→ Memory Candidate
→ Commitment Gate
→ Governed Memory
→ Constraint / Retrieval
→ Future Action
```

其方法论核心是：

> 将长期记忆形成建模为一个具有证据、权限、条件和生命周期的状态转移过程。

EAGLE 由两个闭环组成：

```text
学习闭环：Execution → Evidence → Candidate → Commitment

决策闭环：Preference → Constraint
          Knowledge  → Retrieval
          Constraint + Retrieval → Action
```

---

## 2. 问题定义

### 2.1 Agent 决策环境

设用户集合为 $\mathcal{U}$，Agent 在时刻 $t$ 接收到请求：

$$
Q_t \in \mathcal{Q}
$$

当前场景表示为：

$$
C_t=(app,task,artifact,\ldots)
$$

当前运行环境表示为：

$$
Env_t=(OS,ToolVersion,Schema,Network,\ldots)
$$

Agent 可选择的原始动作空间为：

$$
\mathcal{A}_t
$$

执行动作 $a_t\in\mathcal{A}_t$ 后得到执行结果 $R_t$ 和用户反馈 $U_t$。

### 2.2 Execution Episode

EAGLE 将一次可归因执行过程表示为：

$$
E_t=(Q_t,C_t,A_t,R_t,U_t,Env_t)
$$

工程上的最小充分 Episode 为：

$$
E_t=\{
request,
tool,
argumentsDigest,
result,
error,
retry,
fallback,
userCorrection,
environment
\}
$$

Episode 是证据来源，不是长期记忆本身。

### 2.3 归因歧义

观察到动作 $a_t$ 时，其真实原因可能是：

$$
Z_t\in\{
ExplicitChoice,
AgentDecision,
SystemDefault,
Fallback,
EnvironmentRestriction,
Accident
\}
$$

但系统通常只能观察 $E_t$，不能直接观察潜变量 $Z_t$。因此，从执行轨迹到长期记忆的映射不能采用无条件提升：

$$
E_t\not\Rightarrow M
$$

EAGLE 将其分解为：

$$
E_t\xrightarrow{A_\phi}\mathcal{E}_t
\xrightarrow{Aggregate}M_c
\xrightarrow{G_\theta}S
$$

其中：

- $A_\phi$ 是证据归因函数；
- $\mathcal{E}_t$ 是从 Episode 产生的证据集合；
- $M_c$ 是 Memory Candidate；
- $G_\theta$ 是 Commitment Gate；
- $S$ 是候选状态。

---

## 3. 记忆表示与决策权限

### 3.1 双记忆系统

EAGLE 将长期记忆划分为两个不具有相同权限的集合：

$$
\mathcal{M}=\mathcal{P}\cup\mathcal{K}
$$

Preference 表示用户希望系统如何行动：

$$
P=(Rule,Scope,Hardness,Confidence,Version,Status)
$$

Knowledge 表示系统在特定条件下学到了什么：

$$
K=(Condition,Action,Evidence,Confidence,Version,Status)
$$

其中：

$$
Condition=Scene+Environment+ToolVersion
$$

### 3.2 权限关系

EAGLE 规定：

$$
Authority(P)>Applicability(K)
$$

该关系不意味着发生冲突时删除 Knowledge，而是由 Preference 决定 Knowledge 在当前决策中的可见性：

$$
Visible(K\mid P,C,Env)\in\{0,1\}
$$

因此可以同时满足：

$$
Valid(K)=1
$$

$$
Visible(K\mid P,C,Env)=0
$$

### 3.3 HARD 与 SOFT Preference

Preference 被划分为：

$$
\mathcal{P}=\mathcal{P}_H\cup\mathcal{P}_S
$$

HARD Preference 改变可行动作空间：

$$
\mathcal{A}'_t=\{a\in\mathcal{A}_t\mid a\models\mathcal{P}_H\}
$$

SOFT Preference 不删除合法动作，只改变候选知识或动作的相对顺序：

$$
Rank'(x)=StableRerank(Rank(x),Compatibility(x,\mathcal{P}_S))
$$

---

## 4. Execution Evidence Attribution

### 4.1 证据定义

一条证据表示为：

$$
e_i=(type,direction,strength,condition,source,reason)
$$

其中：

- `type`：Preference 或 Knowledge；
- `direction`：Positive、Negative 或 Neutral；
- `strength`：Strong 或 Weak；
- `condition`：证据成立的 Scene 和 Environment；
- `source`：来源 Episode；
- `reason`：可审计的归因原因。

归因函数为：

$$
A_\phi(E_t)\rightarrow\{e_1,e_2,\ldots,e_n\}
$$

第一阶段的 $A_\phi$ 是显式规则系统，而不是不可解释的端到端分类器。

### 4.2 显式 Preference 证据

对于具有 future-facing 语义的用户声明，例如“以后编辑 DOCX 都用 WPS”，产生：

$$
e_P^{strong+}
$$

其候选内容为：

```text
type      = P
key       = preferred_tool
value     = {tool: wps}
scope     = {task: edit, artifact_type: docx}
hardness  = HARD
explicit  = true
```

自然语言理解模块负责把原始话语转换为结构化 `ExplicitPreferenceEvent`。EAGLE 治理层不直接依赖某个特定 LLM 抽取实现。

### 4.3 隐式稳定选择证据

只有在存在真实选择、且可以判断为用户介入时，成功动作才产生弱 Preference 证据：

$$
e_P^{weak+}
$$

系统默认、Agent 自主选择和被迫执行不能仅凭动作结果产生这一证据。

### 4.4 Fallback 归因

对于：

```text
A failed with error E
→ B succeeded
```

EAGLE 产生条件化 Knowledge 证据：

$$
e_K^+:(A,E,Env)\rightarrow B
$$

且禁止产生 Preference 证据：

$$
FallbackSuccess\not\Rightarrow e_P^+
$$

该规则解决了 OS Agent 中常见的错误提升：备用工具被使用，不代表用户偏爱备用工具。

### 4.5 证据去重

证据重放标识至少包含：

$$
(CandidateId,EpisodeId,Type,Direction,AttributionReason)
$$

`AttributionReason` 必须参与身份判定。否则同一 Episode 中方向相同、但语义不同的显式和隐式证据会被错误合并。

---

## 5. Memory Candidate 与证据聚合

### 5.1 Candidate 身份

证据不直接写入正式记忆，而是聚合到 Candidate：

$$
M_c=(type,key,value,scene,E^+,E^-,confidence,state)
$$

Candidate 的稳定身份为：

$$
id_c=SHA256(
userId\Vert type\Vert key\Vert canonical(value)\Vert canonical(scene)
)
$$

这保证相同用户、相同语义内容和相同作用域的证据进入同一个候选，而不同 Scene 或不同 Value 不被提前合并。

### 5.2 聚合变量

Preference Candidate 至少维护：

```text
positive_evidence
negative_evidence
independent_choices
distinct_sessions
explicit_user_statement
has_unresolved_conflict
```

Knowledge Candidate 至少维护：

```text
positive_evidence
negative_evidence
same_condition_success
user_confirmed
environment_fingerprint
```

### 5.3 Candidate 状态

$$
State(M_c)\in\{
PENDING,
COMMITTED,
REJECTED,
DEFER
\}
$$

- `PENDING`：证据不足；
- `COMMITTED`：获得长期影响权；
- `REJECTED`：证据支持拒绝；
- `DEFER`：存在当前无法自动消解的冲突。

Candidate 是治理缓冲区，其关键语义是：

$$
ObservedPattern\neq CommittedBelief
$$

---

## 6. Commitment Gate

### 6.1 Preference Gate

第一阶段采用可解释的层级规则：

$$
G_P(M_c)=
\begin{cases}
DEFER, & unresolvedConflict=1\\
COMMITTED, & explicit=1\\
COMMITTED, & n\ge3\land s\ge2\land c^-=0\\
PENDING, & otherwise
\end{cases}
$$

其中：

- $n$ 为独立选择次数；
- $s$ 为不同 Session 数；
- $c^-$ 为反向证据数量。

这不是统计最优阈值，而是第一阶段可验证、可解释的治理规则。阈值必须通过消融实验验证，不能被描述为普适常数。

### 6.2 Knowledge Gate

对于 Workflow Knowledge：

$$
G_K(M_c)=
\begin{cases}
COMMITTED, & userConfirmed=1\\
COMMITTED, & repeatedSuccess\ge2\\
PENDING, & otherwise
\end{cases}
$$

因此单次未确认成功不能直接成为长期知识：

$$
SingleSuccess\not\Rightarrow CommittedKnowledge
$$

### 6.3 不采用未经训练的加权总分

第一阶段不使用：

$$
Score=w_1Semantic+w_2Preference+w_3Evidence+w_4Scene
$$

原因不是加权方法无效，而是缺少验证集时，$w_i$ 只会制造不可验证的精确感。EAGLE 首先采用：

$$
HardConstraint
>Scope
>AuthorityState
>EnvironmentValidity
>SemanticSimilarity
>SoftCompatibility
$$

只有在获得带标签的开发集后，才允许学习排序层权重；HARD Constraint 不进入可学习权重。

---

## 7. Governed Memory Commit

### 7.1 单一权威状态

EAGLE 定义：

$$
SQLite=AuthoritativeState
$$

$$
Mem0/Kylin=DerivedSemanticIndex
$$

SQLite 回答“哪些记忆当前有效”，向量索引回答“哪些有效记忆与 Query 相关”。

### 7.2 提交事务

Candidate 晋升必须在单个 SQLite 事务内执行：

```text
BEGIN
Candidate → COMMITTED
Create Preference or Knowledge
Create MemoryEvidenceLink
Create IndexJob if semantic indexing is required
COMMIT
```

正式 Memory 没有 EvidenceLink 时事务失败：

$$
Committed(M)\Rightarrow Evidence(M)\neq\emptyset
$$

### 7.3 Mem0 raw insert

Outbox Worker 只对已提交 Knowledge 执行：

```python
Memory.add(..., infer=False)
```

若使用 `infer=True`，Mem0 会再次执行抽取和 ADD/UPDATE/DELETE 决策，形成两个治理中心。因此：

$$
EAGLECommitted\Rightarrow Mem0RawInsert
$$

### 7.4 幂等索引身份

由于当前 Mem0 raw insert 在内部生成随机 UUID，Outbox 使用确定性业务键：

$$
indexKey=operation:kind:memoryId:version
$$

Worker 在 UPSERT 前按 `indexKey` 查找已有向量。若进程在外部写入成功后、SQLite 写回 `mem0_id` 前崩溃，重试可以重新发现该向量。

DELETE 在 `mem0_id` 缺失时同样按原 UPSERT `indexKey` 查找，从而关闭“孤儿向量无法删除”的崩溃窗口。

---

## 8. Preference Resolution 与约束编译

### 8.1 Scene 匹配

记忆作用域 $Scope(M)$ 与当前 Scene 的匹配定义为：

$$
Match(M,C)=
\bigwedge_{(k,v)\in Scope(M)}C[k]=v
$$

空 Scope 匹配所有 Scene。

### 8.2 Specificity

$$
Specificity(M)=|\{(k,v)\in Scope(M)\}|
$$

对于同时匹配且具有相同 Preference Key 的规则，选择：

$$
P^*=\arg\max_P(Specificity(P),Version(P))
$$

即更具体规则优先；具体度相同时使用更新版本。

### 8.3 Constraint Compilation

HARD Preference 被编译为结构化约束：

```text
allowed_tools
denied_tools
require_offline
allowed_formats
privacy_rules
```

Planner 接收的工具集合必须经过物理过滤：

$$
Tools'=Filter(Tools,Compile(P_H))
$$

未知 HARD Preference Key 必须快速失败，不能静默降级为 Prompt 文本。

---

## 9. PACK：面向决策的记忆组装

### 9.1 输入输出

定义：

$$
PACK(Q,C,Env)\rightarrow(Constraints,Knowledge,Evidence)
$$

PACK 不是简单向量检索接口，而是把权威状态、约束和条件化知识组装为 Planner Context 的过程。

### 9.2 执行顺序

```text
1. SQLite 查询 ACTIVE Preference
2. Scene Resolution 与 Version Collapse
3. HARD Constraint Compilation
4. Mem0/Kylin Knowledge Retrieval
5. SQLite ACTIVE Knowledge Intersection
6. P-K Visibility Filter
7. Scene Filter
8. Environment Validation
9. SOFT Preference Stable Rerank
10. Attach Evidence
```

顺序不能任意交换。尤其 HARD Preference 必须先于 Planner 动作选择，SQLite ACTIVE Intersection 必须晚于近似召回并早于 Planner。

### 9.3 权威态交集

设向量召回集合为 $K_v$，SQLite 当前 ACTIVE 集合为 $K_a$，最终候选至少满足：

$$
K_c=K_v\cap K_a
$$

因此即使向量删除延迟、状态 metadata 陈旧或过滤能力有限，非 ACTIVE Knowledge 也不能进入 Planner。

### 9.4 环境有效性

对每条 Knowledge：

$$
EnvironmentMatch(K,Env_t)=
\begin{cases}
1,& Env_K=Env_t\\
0,& otherwise
\end{cases}
$$

第一阶段采用精确匹配。若不匹配：

```text
ACTIVE → NEEDS_REVALIDATION
```

并从当前 PACK 中排除。该规则偏向 Precision；后续可以基于验证数据定义 minor drift 与 major drift，但不能预先假定哪些环境字段可以忽略。

### 9.5 P-K Visibility

定义掩蔽关系：

$$
Mask(P_i,K_j,reason)\in\{0,1\}
$$

PACK 对当前有效 Preference 计算：

$$
K_{visible}=\{K\in K_c\mid \nexists P,Mask(P,K)=1\}
$$

Preference 撤销或遗忘后，对应 Mask 失效，Knowledge 可以重新可见，无需重新学习或重建内容。

---

## 10. 生命周期、版本与遗忘

### 10.1 不可变版本

正式记忆使用版本链：

$$
M_1\rightarrow M_2\rightarrow\cdots\rightarrow M_n
$$

每个新版本记录：

```text
lineage_id
version
parent_version_id
status
```

Preference 同 Scope、同 Key、不同 Value 的显式更新会撤销旧版本并创建新版本，而不是覆盖原记录。

### 10.2 Logical Forget

对于带向量索引的 Knowledge：

```text
ACTIVE → FORGETTING
```

事务提交后，PACK 只读取 ACTIVE 状态，因此：

$$
Status(K)=FORGETTING\Rightarrow Recall(K)=0
$$

该性质不依赖外部向量删除是否已经完成。

### 10.3 Physical Erase

Outbox Worker 完成 Mem0 删除后：

```text
mem0_id = NULL
FORGETTING → FORGOTTEN
```

删除失败时保持 `FORGETTING`，并暴露 FAILED Job；系统不能提前报告物理删除完成。

当前实现不为 Preference 建立 Mem0 索引，因此 Preference 遗忘可以在单个 SQLite 事务内失效，并同时解除相关 P-K Visibility。

---

## 11. 方法算法

### Algorithm 1：从 Episode 学习治理记忆

```text
Input:
    episode E
    explicit preference events X

Output:
    candidate states and committed memory ids

1: BEGIN SQLite transaction
2: episode_id ← PersistEpisode(E)
3: evidence_set ← RuleAttribution(E)
4: evidence_set ← evidence_set ∪ ExplicitAttribution(X)
5: for each evidence e in evidence_set do
6:     candidate ← AggregateByStableIdentity(e)
7:     DeduplicateEvidence(candidate, episode_id, e.reason)
8:     UpdateCandidateStatistics(candidate, e)
9:     DetectPreferenceConflict(candidate)
10:    decision ← CommitmentGate(candidate)
11:    if decision = COMMITTED then
12:        memory ← CreateImmutableGovernedMemory(candidate)
13:        LinkAllEvidence(memory, candidate)
14:        if memory.type = Knowledge then
15:            CreateIndexJob(UPSERT, memory)
16:        end if
17:    else
18:        candidate.state ← decision
19:    end if
20: end for
21: COMMIT
22: return episode_id, candidate_ids, committed_memory_ids
```

### Algorithm 2：构建 Planner Context

```text
Input:
    query Q
    user U
    scene C
    environment Env
    limit k

Output:
    PlannerContext

1: preferences ← SQLite.ACTIVE_P(U)
2: preferences ← ResolveScopeAndVersion(preferences, C)
3: constraints ← CompileHardPreferences(preferences)
4: recalled ← Mem0.search(Q, filters={user_id: U, memory_kind: K}, 4k)
5: authoritative ← SQLite.ACTIVE_K(U, ids(recalled))
6: blocked ← ActiveVisibilityMasks(preferences)
7: eligible ← []
8: for each item in recalled order do
9:     K ← authoritative[item.eagle_memory_id]
10:    if K does not exist or K.id in blocked then continue
11:    if not SceneMatch(K, C) then continue
12:    if not EnvironmentMatch(K, Env) then
13:        K.status ← NEEDS_REVALIDATION
14:        continue
15:    end if
16:    eligible.append(K, item.semantic_score)
17: end for
18: eligible ← StableSoftPreferenceRerank(eligible)
19: selected ← first k items from eligible
20: evidence ← EvidenceLinks(selected)
21: return PlannerContext(constraints, selected, evidence)
```

### Algorithm 3：异步遗忘 Knowledge

```text
Input:
    knowledge id K

1: BEGIN SQLite transaction
2: require K.status = ACTIVE
3: K.status ← FORGETTING
4: CreateIndexJob(DELETE, K, target_mem0_id=K.mem0_id)
5: COMMIT
6: PACK can no longer return K
7: Worker resolves vector by mem0_id or deterministic UPSERT index_key
8: Worker calls Mem0.delete(vector_id)
9: BEGIN SQLite transaction
10: K.mem0_id ← NULL
11: K.status ← FORGOTTEN
12: DELETE job ← DONE
13: COMMIT
```

---

## 12. 条件性理论性质

以下性质是系统设计在明确前提下提供的保证，不是对所有外部组件的无条件证明。

### Proposition 1：Fallback Non-Promotion

若 Attribution 模块满足：

$$
A_\phi(FallbackSuccess)\cap Evidence_P=\emptyset
$$

且没有其他独立 Preference Evidence，则 fallback Episode 不会使 Preference Candidate 晋升。

证明思路：Candidate 的 Preference 统计量只能由 Preference Evidence 更新；fallback 规则只产生 Knowledge Evidence，因此 Preference Gate 的输入不因 fallback 增长。

### Proposition 2：Commitment Isolation

若正式 Preference/Knowledge 表只由 Gate 的 COMMITTED 分支写入，PACK 只读取正式 ACTIVE 表，则：

$$
State(M_c)=PENDING\Rightarrow Recall(M_c)=0
$$

### Proposition 3：Hard Constraint Safety

若：

1. 所有 Planner 工具都来自 `apply_constraints` 的输出；
2. 不存在绕过该输出直接调用工具的执行通道；
3. Constraint Compiler 对未知 HARD Key 快速失败；

则：

$$
a\not\models P_H\Rightarrow a\notin\mathcal{A}'
$$

该保证依赖“所有工具执行入口统一”这一系统前提，仅把规则写进 Prompt 不能得到该性质。

### Proposition 4：Logical Forget Safety

若状态变更与 PACK 查询使用同一 SQLite 权威库，且 PACK 仅查询 ACTIVE，则：

$$
Status(M)\in\{FORGETTING,FORGOTTEN\}
\Rightarrow Recall(M)=0
$$

该性质不依赖向量存储的删除延迟。

### Proposition 5：Traceability

若 Memory 创建、EvidenceLink 创建和 Candidate COMMITTED 在同一事务中，且无 Evidence 时事务失败，则：

$$
Committed(M)\Rightarrow Evidence(M)\neq\emptyset
$$

### Proposition 6：派生索引最终收敛

若满足：

1. 外部 Mem0/Kylin 最终可用；
2. Worker 失败可重试；
3. `indexKey` 查询满足 read-after-write；
4. Reconciliation 在 Worker 启动前运行；

则 ACTIVE 且缺少索引的 Knowledge 最终会产生索引，FORGETTING 且残留索引的 Knowledge 最终会删除索引。

这是一项 eventual convergence 性质，不是跨 SQLite 与向量引擎的原子提交保证。

---

## 13. 实验方法

### 13.1 研究问题

建议将实验组织为以下 Research Questions：

```text
RQ1  Evidence Attribution 是否降低 Preference 误提升？
RQ2  Commitment Gate 是否提高正式记忆精度？
RQ3  HARD Constraint Compilation 是否降低行为违规率？
RQ4  Environment Validation 是否降低过期知识复用？
RQ5  P-K Visibility 是否在保留知识的同时降低冲突执行？
RQ6  Outbox + authoritative post-filter 是否控制删除泄漏？
```

### 13.2 对比系统

至少设置三个可比基线：

```text
B0  Mem0 original inference
B1  Mem0 raw memory + simple preference extraction
B2  Mem0 + EAGLE full governance
```

为避免归因不公平，三个系统应共享：

- 相同基础 Agent；
- 相同工具集合；
- 相同 Embedding 和 VectorStore；
- 相同 Query、Episode 顺序和环境；
- 相同检索候选规模。

### 13.3 数据划分

测试数据不能仅随机拆分单条 Episode。应按用户、Session 和环境组合划分，以避免相邻行为泄漏：

```text
Train/Development：用于确定规则和阈值
Validation：用于选择阈值或排序权重
Test：冻结方法后仅做最终报告
```

环境漂移实验必须包含：

```text
same environment
minor version drift
major tool/schema drift
network capability change
```

### 13.4 场景集合

最小评测场景应包括：

1. 用户显式声明工具偏好；
2. 多次自主选择形成隐式偏好；
3. 工具失败后 fallback 成功；
4. 系统默认工具连续成功；
5. 用户纠正既有偏好；
6. 一般 Preference 与具体 Scene Preference 并存；
7. Knowledge 与 HARD Preference 冲突；
8. 工具版本变化导致旧 Knowledge 失效；
9. 遗忘请求后向量删除延迟；
10. Outbox 在外部写成功、SQLite 写回前崩溃。

### 13.5 指标

Preference Precision：

$$
PreferencePrecision=
\frac{CorrectCommittedP}{AllCommittedP}
$$

False Promotion Rate：

$$
FPR_P=
\frac{NonPreferencePromotedToP}{AllNonPreferenceCases}
$$

Knowledge Precision：

$$
KnowledgePrecision=
\frac{CorrectApplicableK}{AllReturnedK}
$$

Hard Constraint Violation Rate：

$$
HCVR=
\frac{ActionsViolatingHardPreference}{ActionsUnderHardPreference}
$$

Stale Knowledge Reuse Rate：

$$
SKRR=
\frac{ExecutedStaleKnowledge}{AllEnvironmentDriftCases}
$$

Forget Leakage Rate：

$$
FLR=
\frac{ForgottenMemoryReturnedAfterLogicalForget}{AllForgetQueries}
$$

Traceability Coverage：

$$
TC=
\frac{CommittedMemoryWithEvidence}{AllCommittedMemory}
$$

此外还应报告：

```text
Knowledge Recall@K
Conflict Resolution Accuracy
Downstream Task Success
PACK latency
Index convergence delay
Memory count and storage cost
```

### 13.6 统计报告

对于比例指标，应报告置信区间，而不只报告单个百分比。对于同一任务上的系统比较，应使用配对评估，因为不同方法面对的是同一组 Episode 和 Query。

若测试集规模不足，不应把小数点后的微小差异解释为方法优势。

---

## 14. 消融实验

### A1：移除 Evidence Attribution

```text
Episode → direct memory extraction
```

观察 `PreferenceFalsePromotionRate`，重点检查 fallback 和默认行为。

### A2：移除 Commitment Gate

```text
first positive evidence → COMMITTED
```

观察 Memory Precision、Memory Recall 和下游任务成功率的变化。

### A3：HARD Preference 降级为 Prompt

```text
Constraint Compiler → prompt instruction
```

观察 Hard Constraint Violation Rate，以验证物理动作空间过滤的必要性。

### A4：移除 Environment Validation

所有语义相似 Knowledge 均可进入 Planner，观察 Stale Knowledge Reuse Rate。

### A5：P-K 冲突直接删除 Knowledge

与 Visibility Mask 对比，观察 Preference 撤销后的 Knowledge 恢复能力和 Knowledge Retention。

### A6：移除 SQLite authoritative intersection

直接信任向量 metadata 状态，重点测试 FORGETTING、删除延迟和索引状态陈旧场景。

### A7：不同 Gate 阈值

比较：

```text
n = 1, 2, 3, 4
session constraint on/off
negative evidence constraint on/off
```

绘制 Memory Precision、Recall 与 Downstream Success 的变化，而不是只选择对 EAGLE 最有利的单点。

---

## 15. 有效性威胁与方法边界

### 15.1 Attribution 依赖可观测信息

如果 Agent 无法区分用户主动选择与系统默认动作，隐式 Preference 归因不可靠。方法不能用 Gate 修复缺失的因果信号。

### 15.2 阈值不具有普适性

“三次选择、两个 Session”和“两次同条件成功”是初始治理规则，不是理论最优值。不同工具风险、任务频率和用户行为可能需要不同阈值。

### 15.3 环境精确匹配偏保守

当前环境匹配会牺牲部分 Knowledge Recall，以避免未经验证的旧知识执行。若引入环境相似度，需要单独标注哪些漂移不影响知识有效性。

### 15.4 HARD Safety 依赖统一执行入口

如果存在绕过 `apply_constraints` 的工具执行路径，HARD Constraint Safety 不成立。因此工具注册、Planner 和 Executor 的集成测试属于方法验证的一部分。

### 15.5 SQLite 与向量引擎不是分布式事务

Outbox 提供最终收敛和立即逻辑不可见，不提供两个存储之间的瞬时原子一致性。物理删除完成时间必须单独测量。

### 15.6 当前真实 Kylin SDK 未验证

Provider 目前基于严格 adapter contract。距离定义、过滤语义、线程安全性、read-after-write 和分页行为必须在真实 SDK 上验证后，才能报告真实系统指标。

---

## 16. 方法与代码映射

| 方法组件 | 实现位置 |
|---|---|
| Episode | `eagle_os_agent/eagle/episode/collector.py` |
| Evidence Attribution | `eagle_os_agent/eagle/attribution/rule_based.py` |
| Candidate Aggregate | `eagle_os_agent/eagle/candidate/service.py` |
| Commitment Gate | `eagle_os_agent/eagle/gate/commitment.py` |
| Governed Commit | `eagle_os_agent/eagle/governance.py` |
| Preference Version | `eagle_os_agent/eagle/preference/service.py` |
| HARD Compiler | `eagle_os_agent/eagle/preference/compiler.py` |
| PACK | `eagle_os_agent/eagle/pack/service.py` |
| P-K Visibility | `eagle_os_agent/eagle/conflict/service.py` |
| Forgetting | `eagle_os_agent/eagle/forgetting/service.py` |
| Outbox/Reconciliation | `eagle_os_agent/eagle/outbox/` |
| Mem0 Boundary | `eagle_os_agent/adapters/mem0_gateway.py` |
| Kylin Provider | `mem0/embeddings/kylin.py`、`mem0/vector_stores/kylin.py` |
| Inference Guard | `mem0/llms/noop.py` |

---

## 17. 方法论总结

EAGLE 的完整状态更新可以写为：

$$
M_{t+1}=G(M_t,A(E_t),C_t,Env_t,U_t)
$$

Agent 决策写为：

$$
Action_t=
\pi(
Q_t,
Compile(P_t),
Retrieve(K_t\mid C_t,Env_t,P_t)
)
$$

两个公式分别描述：

1. 历史执行如何经过证据治理改变长期记忆状态；
2. 已治理记忆如何以不同权限影响未来行为。

因此，EAGLE 的方法论不是“提取—存储—召回”，而是：

```text
归因什么
→ 相信多少
→ 何时生效
→ 拥有什么权限
→ 在什么条件下可用
→ 如何撤销和审计
```

其目标不是最大化记忆数量，而是同时优化：

$$
MemoryCorrectness
+MemoryApplicability
+DecisionSafety
+Traceability
$$
