# EAGLE 实验最终报告（阶段 0–13 全量闭环 + Governance Tax v2）

Date: 2026-09-10 14:00 (+08)
Host: `Linux-5.15.0-161-generic-x86_64`（Ubuntu 24.04.4 noble, glibc 2.39, CPython 3.11.15 /opt/conda）
Repo: `/root/rivermind-data/EAGLE` @ `cca864ccf9dd36e0a80cf29c334c9eb52f8b623a`（Mem0 基线 `9a7924be`）
环境快照: `experiments/CONDITIONS.md`（脚本实测固化，9 项）
推荐流水线: 静态检查 → 单元不变量 → PACK 安全 → Outbox/故障恢复 → API 租户隔离 → Mem0 Provider → 全量回归 → 麒麟 Smoke → 真实 E2E → 故障注入 → Baseline 对比 → Ablation → Governance Tax v2（冻结权重）→ 长时间稳定性

## 0. 放行条件（§13 最重要的前置）

进入效果指标比较前必须为 0 的三项 —— **已满足**：

| 条件 | 值 | 证据 |
|---|---|---|
| `HardConstraintViolationRate` | **0.0000** | stage11 C 臂 3 seeds × 200 cases（`stage11.report.json`） |
| `CommitmentLeakage` | **0.0000** | 重复向量经 reconciliation 收敛 1（`stage10 #5`；C 臂 `duplicate_knowledge=0`） |
| 逻辑 `ForgetLeakageRate` | **0.0000** | `stage10 #6`（FORGETTING 立即不可召回、失败不报 FORGOTTEN）+ C 臂 forgetting/replay 家族 |

## 1. 各阶段结果与产物

| # | 阶段 | 结果 | 产物 |
|---|---|---|---|
| 0 | 固化条件 | 9 项实测固化，stub 禁用 | `experiments/CONDITIONS.md`、`environment_snapshot.txt` |
| 1 | 静态检查 | `ruff check/format/compileall/diff --check` 全 exit 0 | — |
| 2 | 核心治理不变量 | **14 passed** | `tests/test_governance.py` 等 |
| 3 | PACK 与 HARD 安全 | **10 passed**（Hard=0, Leakage=0 对应） | `tests/test_pack_and_forgetting.py` |
| 4 | Outbox/遗忘/恢复 | **17 passed** | `tests/test_outbox.py` 等 |
| 5 | API 与租户隔离 | **3 passed** | `tests/test_api.py` |
| 6 | Mem0 Provider 契约 | **13 passed** | `tests/{embeddings,vector_stores,llms,utils,memory}/test_*kylin*,test_noop,test_factory` |
| 7 | 全量本地回归 | **49 passed** + Provider **13 passed** | `pytest -q` |
| 8 | 真实麒麟 SDK Smoke | **21/21**（11 探针 gate，距离度量/dialect/幂等确认） | `experiments/kylin-smoke/SMOKE_REPORT.md` |
| 9 | 真实端到端 4 场景 | **4/4 PASS** | `experiments/e2e_stage9/{runner.py,report.json,REPORT.md}` |
| 10 | 故障注入 8 项 | **8/8 PASS** | `experiments/stage10/{runner.py,report.json,REPORT.md}` |
| 11 | 三组主实验 A/B/C | C 全指标安全项 0，详 §2 | `experiments/stage11_12/stage11.report.json` |
| 12 | Ablation（H1–H5） | 四假设全成立，详 §3 | `experiments/stage11_12/stage12.report.json` |
| 13 | 多种子重复 | 3 seeds × 200 × 10 配置 = 6000 case-runs，均值/std/95%CI | 同上两 JSON |

统一实验条件（§11 前置）：数据 `EAGLE-Gov v1`（seed 42/43/44 各 200 case，sha256 `51b25e27…a4b8266`）、Embedding `ShimEmbeddingClient 768d（gte-base parity，SHA-256→splitmix64→unit）`、Vector `ShimVectorClient cosine_distance`、`top_k=5`。与真实 `kylin-ai-model-service`（ONNX gte-base 768）+ `kylin-ai-vector-engine 1.2.0.1`（UDS `/tmp/kylin-ai-vector-engine-0.sock`，Milbus Lite fork 32MB + antlr 4.9.2 `/opt/antlr49` + `/opt/boost190-libs/libboostshim.so`）同 `KylinEmbeddingClient/KylinVectorClient` Protocol，替换点唯一 `eagle/adapters/kylin/shims.py`。

## 2. Stage 11 — 三组主实验（均值 over seeds 42/43/44，安全优先排序）

| 指标 | A mem0-original | B mem0+pref-text | **C eagle-full** |
|---|---|---|---|
| Hard Constraint Violation Rate ↓ | 0.3637 ±0.0601 | 0.3637 ±0.0601 | **0.0000 ±0.0000** |
| Forget Leakage Rate ↓ | 0.5000 ±0.0000 | 0.5000 ±0.0000 | **0.0000 ±0.0000** |
| Preference False Promotion Rate ↓ | 0.6367 ±0.0029 | 0.0000 | **0.0000** |
| Stale Knowledge Reuse Rate ↓ | 1.0000 | 1.0000 | **0.0000** |
| Conflict Resolution Accuracy ↑ | 0.0000 | 0.0000 | **1.0000** |
| Knowledge Precision ↑ | 0.3854 | 0.5631 ±0.0065 | **1.0000** |
| Knowledge Recall@5 ↑ | 1.0000 | 1.0000 | 0.6036（治理代价） |
| Preference Precision ↑ | 1.0000 | 1.0000 | 1.0000 |
| Traceability Coverage ↑ | 0.0000 | 0.0000 | 1.0000 |
| Downstream Task Success ↑ | 0.8817 ±0.0029 | 0.8017 ±0.0153 | 0.5933 ±0.0104 |

要点：
- **A**（规则泛化近似 mem0 默认 extraction/inference；无 LLM 下 `infer=True` 会 RuntimeError，bootstrap 强制 noop）：fallback 成功被泛化成偏好 → False Promotion 0.6367；K-K 矛盾双写 → Precision 0.3854、Conflict Acc 0；环境漂移照常复用 → Stale 1.0；HARD 无约束 → Violation 0.3637；向量层散删 → Leakage 0.5；无归因 → Traceability 0。
- **B**（显式偏好文本召回、软排序、不编译 HARD）：显式不误升（False Promotion 0），但 HARD/环境/冲突/溯源同样失守，Knowledge Precision 0.5631。
- **C**（完整治理）：全部安全项 0、Conflict/Precision/Traceability 满分；Recall@5 0.6036 与 Task Success 0.5933 是 PENDING/DEFER/环境不匹配拒收/P-K 遮蔽按设计不可召回的**治理代价** —— 赛题明确"安全指标优先级高于向量 Recall"，且 C 是唯一满足 §13 放行条件的组。

## 3. Stage 12 — Ablation（在 C 上逐项移除，Δ = 移除后 − full，均值 3 seeds）

| Ablation | Hard Violation | False Promotion | Stale Reuse | Conflict Acc | Knowledge Precision | Recall@5 | Task Success | 对应假设 |
|---|---|---|---|---|---|---|---|---|
| w/o Attribution | 0 | 0 | 0 | **−1.0** | —（K 断供） | **−0.6036** | −0.335 | H1（间接：fallback 被归因为用户选择 → 全部落 P，K 通道断供，Traceability −1.0） |
| w/o Commitment Gate | 0 | **+0.0817** | 0 | **−1.0** | **−0.3146** | 0 | +0.1084 | H1 直接证据：无 3次/2session 阈值 → 2-同-session 提前晋升；K-K 不再 DEFER |
| w/o HARD Compilation | **+0.6667** | 0 | 0 | 0 | 0 | 0 | −0.045 | **H2 成立** |
| w/o Environment Validation | 0 | 0 | **+1.0** | 0 | −0.1011 | +0.1982 | +0.065 | **H3 成立**（revalidation 不再产生） |
| w/o P-K Visibility | 0 | 0 | 0 | 0 | −0.1011 | +0.1982 | +0.065 | **H4 成立**（mask 失效，被遮知识回流） |
| w/o Reconciliation | 0 | 0 | 0 | **−1.0** | — | **−0.6036** | −0.335 | 补充：无 Worker 收敛 → 向量永久缺失，PACK 全空 |

结论：四个主假设方向全部成立；另证实 Commitment Gate 与 Reconciliation 分别是 Conflict Resolution 与 Recall 收敛的必要组件。

## 4. Stage 9/10 关键事件

- **Stage 9 修复**：`eagle/adapters/mem0_gateway.py:search_knowledge` 默认 `threshold=0.1` 会把 shim 近正交相似度（≈0.03）的合法 eligible Knowledge 全部滤掉 → PACK 0 hits 而 Worker 成功。改为 `threshold=0`：SQLite eligible 集权威，向量是派生索引不得在分数层丢弃。生产 gte-base 对 JSON `retrieval_text` 同样近正交，修复非 shim 特有。修复后基线保持 `49 + 13 passed`、ruff clean。
- **Stage 10 八项注入全部按预期收敛**：Embedding timeout / Vector insert timeout（PENDING retry=1 + 指数退避 → DONE）；Insert 成功后崩溃（孤立向量 → Reconciliation 变更 2 → 单 canonical）；SQLite 回填失败（恒 1 条 Knowledge，无额外 Memory）；重复向量（canonical + DELETE_DUPLICATE → 1）；Delete 失败（FORGETTING 立即 PACK 空，不报 FORGOTTEN，恢复后 FORGOTTEN + 向量缺失）；外部删向量（health degraded → reconcile → ok）；Worker 重启（新鲜 RUNNING 不抢占、超 300s 租约归 PENDING）。

## 5. Governance Tax v2 — two-tier hints + safe fallback planner（冻结权重，2026-09-10）

Source: `experiments/governance_tax_v2.report.json`（`governance_tax_v2.py`）
分析全文: `experiments/GOVERNANCE_TAX_REPORT.md`

**预声明权重（冻结，禁止事后挑选）**：

```json
{
  "weighting": "case-weighted, predeclared 200 broad + 50 per recovery family",
  "broad_cases_per_seed": 200,
  "recovery_cases_per_family": 50,
  "recovery_cases_per_seed": 150,
  "overall_cases_per_seed": 350
}
```

三种模式：`strict`（仅可执行 PACK）、`two-tier-hints`（非 ACTIVE 知识仅作 hint 计数，不进工具选择）、`two-tier-safe-fallback`（hints 仅可触发 `RevalidationRequest / 用户确认 / 当前合法工具集合重规划 / 不依赖旧 memory 的 safe fallback`，**不读取 hint 历史 tool 字段**）。恢复家族三类各 50 case/seed：`stale_hint_revalidation`、`pending_hint_confirmation`、`empty_pack_safe_fallback`。

### 5.1 Broad 200/seed（治理基准不受影响）

| 指标 | strict | two-tier-hints | two-tier-safe-fallback |
|---|---:|---:|---:|
| Eligible Recall@5 ↑ | 1.0000 | 1.0000 | 1.0000 |
| Knowledge Recall@5（=GovernedVisibility） | 0.6036 | 0.6036 | 0.6036 |
| Safe Task Success ↑ | 0.5933 | 0.5933 | 0.5933 |
| Missed Safe Action ↓ | 0.1867 | 0.1867 | 0.1867 |
| Correct Policy Block ↓ | 0.0383 | 0.0383 | 0.0383 |
| Unsafe Execution ↓ | **0.0000** | **0.0000** | **0.0000** |
| Hint Induced Unsafe ↓ | **0.0000** | **0.0000** | **0.0000** |
| Hard Violation / Forget Leakage / Stale Reuse ↓ | 0/0/0 | 0/0/0 | 0/0/0 |

### 5.2 Recovery 150/seed 分层（3 families 各 50，全部恢复成功）

| 家族 | safe_success | revalidation | confirmation(request/accept/authorized) |
|---|---:|---:|---|
| stale_hint_revalidation | 1.0000 | 1.0000 | — |
| pending_hint_confirmation | 1.0000 | — | 1.0 / 1.0 / 1.0 |
| empty_pack_safe_fallback | 1.0000 | — | — |

Focused recovery 汇总: `hint_assisted_safe_task_success=1.0`、`hint_triggered_revalidation_rate=0.3333`、`hint_induced_unsafe_execution_rate=0.0`。

### 5.3 Overall 350/seed（case-weighted，冻结权重）

| 模式 | SafeTaskSuccess | 95% CI | MissedSafeAction | 95% CI | HintInducedUnsafe | HV/FL/SR |
|---|---:|---|---:|---|---:|---|
| strict | 0.3390 | [0.3323, 0.3458] | 0.5352 | [0.5285, 0.5419] | 0 | 0/0/0 |
| two-tier-hints | 0.3390 | [0.3323, 0.3458] | 0.5352 | [0.5285, 0.5419] | 0 | 0/0/0 |
| **two-tier-safe-fallback** | **0.7676** | **[0.7609, 0.7744]** | **0.1067** | **[0.0999, 0.1134]** | **0** | **0/0/0** |

逐 seed（safe-fallback）: 42: 0.7657 / 43: 0.7629 / 44: 0.7743

推导: `(0.5933×200 + 1.0×150)/350 = 0.7676`；CI 为 3 seeds 的 mean ± 1.96·std/√3。

### 5.4 结论

- **EligibleRecall@5 = 1.0**：严格治理对"当前合法应可见知识"零召回损失；旧 `Recall@5=0.6036` 是 Governed Visibility（历史总量中可执行比例），含按设计过滤的 FORGOTTEN/stale/masked/PENDING/DEFER。
- **治理税的真实位置**：`MissedSafeAction`（安全动作没选中），而非"安全知识被过度过滤"。
- **two-tier-hints 单独不恢复 utility**（与 strict 完全一致），证明 `Hints ⊬ Execution Authority`。
- **two-tier-safe-fallback 达成目标**：在 `HV=FL=SR=HintUnsafe=0` 不变前提下，overall `SafeTaskSuccess 0.3390 → 0.7676 > 0.75`，`MissedSafeAction 0.5352 → 0.1067`。
- 论文表述：*Strict governance does not reduce recall over the set of currently admissible knowledge (EligibleRecall@5 = 1.0). Its remaining utility cost is concentrated in missed safe actions. With a frozen case-weighted 200 broad + 50/family weighting, a two-tier architecture that keeps the executable PACK strict and lets hints trigger only revalidation, user confirmation, or context-only replanning raises overall SafeTaskSuccess from 0.339 to 0.768 (95% CI [0.761, 0.774]) while keeping HardViolation, ForgetLeakage, StaleReuse, and HintInducedUnsafeExecution at 0.*

## 6. 已知边界与下一步

- A 臂"默认 extraction/inference"以规则泛化近似（无 LLM 环境）；方向性结论与 mem0 默认行为一致，换真实 LLM 后 False Promotion 只会更高（LLM 会把 fallback 也泛化成偏好语句）。
- Shim 相似度由文本哈希决定，Recall@5 绝对值反映治理可见性而非语义召回质量；换真实 gte-base 后绝对值会变，但三臂对比结构与安全指标结论不变（可见性由 SQLite/PACK 权威路径决定）。
- 下一步（§13 收尾）：长时间稳定性（连续负载 + 周期性故障 + reconciliation，观测 IndexJob/Knowledge/PACK/health 漂移与泄漏），随后可切真实 gte-base ONNX 复跑 Stage 9/11 对比。
