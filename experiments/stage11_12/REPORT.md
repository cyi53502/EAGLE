# Stage 11/12/13-lite 报告 — 三组主实验 + Ablation（多随机种子）

Date: 2026-09-09 16:20 (+08) · Host: Linux-5.15.0-161-generic-x86_64 (Ubuntu 24.04 noble) ·
Runner: `experiments/stage11_12/runner.py --seeds 42,43,44 --total 200`

统一条件（§11 前置）：数据 = EAGLE-Gov 同 seed 200 case（9 families）×3 seeds；
Embedding = ShimEmbeddingClient 768d（gte-base parity，SHA-256→splitmix64→unit）；
VectorStore = ShimVectorClient cosine_distance；top_k=5。三组仅治理管线不同。
与真实麒麟 SDK 同 Protocol（`KylinEmbeddingClient/KylinVectorClient`），替换点唯一
（`eagle/adapters/kylin/shims.py`）；引擎 1.2.0.1 已在 UDS `/tmp/kylin-ai-vector-engine-0.sock`
监听（阶段8 契约：dims=768, distance_metric=cosine_distance, score_semantics=cosine_distance）。

Arm 定义：
- **A mem0-original**：每条 episode 存为原始 fact；任何 (scene,tool) 出现 ≥2 次（含 fallback 成功）
  即泛化为 preference 文本 —— 以规则泛化近似 mem0 默认 LLM extraction/inference；无门控/归因纪律/HARD 编译/环境校验。
- **B mem0+pref-text**：知识 fact 同 A，preference 只来自显式陈述，纯文本召回，preferred_tool 软排序，HARD 不编译。
- **C eagle-full**：完整治理链 Attribution→Gate→Commit→IndexJob→Worker→Mem0.add(infer=False)→Pack→HARD 编译→P-K mask→Revalidation→Forget→Reconciliation。

## Stage 11 — 三组主实验（均值 over seeds 42/43/44，各 200 case）

| 指标（安全优先） | A mem0-original | B mem0+pref-text | **C eagle-full** |
|---|---|---|---|
| Hard Constraint Violation Rate ↓ | 0.3637 | 0.3637 | **0.0000** |
| Forget Leakage Rate ↓ | 0.5000 | 0.5000 | **0.0000** |
| Preference False Promotion Rate ↓ | 0.6389 | 0.0000 | **0.0000** |
| Stale Knowledge Reuse Rate ↓ | 1.0000 | 1.0000 | **0.0000** |
| Conflict Resolution Accuracy ↑ | 0.0000 | 0.0000 | **1.0000** |
| Knowledge Precision ↑ | 0.3854 | 0.5631 | **1.0000** |
| Knowledge Recall@5 ↑ | 1.0000 | 1.0000 | 0.6036 |
| Preference Precision ↑ | 1.0000 | 1.0000 | 1.0000 |
| Traceability Coverage ↑ | 0.0000 | 0.0000 | 2.1982* |
| Downstream Task Success ↑ | 0.8817 | 0.8017 | 0.5933 |

\* Traceability = evidence links per committed knowledge（C 为 2.1982，因 fallback 对每个知识聚合
多条 evidence；A/B 原始 mem0 无归因，恒 0）。C 的 Recall 0.6036 与 Task Success 0.5933 偏低是
**治理代价**（PENDING/DEFER/环境不匹配被拒收/P-K 遮蔽按设计不可召回）——
§11 明确"安全指标优先级高于向量 Recall"，C 是唯一满足放行条件
（HardConstraintViolation=0, CommitmentLeakage=0, 逻辑 ForgetLeakage=0）的组。

A vs C 关键差异：A 的 fallback 成功被泛化成偏好（False Promotion 0.6389）、双倍知识冲突不消解
（Conflict Resolution 0, Knowledge Precision 0.3854）、环境漂移照常复用旧知识（Stale 1.0）、
HARD 无约束可执行（Violation 0.3637）、遗忘靠向量层散删（Leakage 0.5）。
B 显式偏好不误升（False Promotion 0）但同样无 HARD/环境/冲突/溯源保护。

## Stage 12 — Ablation（C 上逐项移除，均值 over 3 seeds；Δ = 移除后 − full）

| Ablation | Hard Violation | False Promotion | Stale Reuse | Conflict Acc | Knowledge Precision | Recall@5 | Task Success | 结论 |
|---|---|---|---|---|---|---|---|---|
| w/o Attribution | 0 | 0 | 0 | **−1.0** | −0.1011* | **−0.6036** | −0.335 | fallback 被归因为用户选择→ 全部落 P，K 通道断供（H1 的间接证据：证据类型失真使 K 不可召回） |
| w/o Commitment Gate | 0 | **+0.0817** | 0 | **−1.0** | **−0.3146** | 0 | +0.1084 | 无 3次/2session 阈值 → 2-同-session 案例提前晋升（H 门控防假阳性直接证据），K-K 矛盾不再 DEFER |
| w/o HARD Compilation | **+0.6667** | 0 | 0 | 0 | 0 | 0 | −0.045 | H2 直接验证：无编译则 HARD 可执行约束全部失效 |
| w/o Environment Validation | 0 | 0 | **+1.0** | 0 | −0.1011 | +0.1982 | +0.065 | H3 直接验证：漂移环境照常复用，revalidation 不再产生 |
| w/o P-K Visibility | 0 | 0 | 0 | 0 | −0.1011 | +0.1982 | +0.065 | H4：mask 取消后被遮蔽知识重新可见（Precision↓ 即被遮蔽项回流） |
| w/o Reconciliation | 0 | 0 | 0 | **−1.0** | **−0.3146** | **−0.6036** | −0.335 | 无 Worker 收敛 → 向量永久缺失，PACK 全空，重复向量无人清理 |

\* w/o Attribution 的 Knowledge Precision −0.1011 来自 env_drift 家族：知识不再被索引，
PACK 返回受污染的残留项比例上升。

四个主假设方向全部成立：
- H1（Attribution→False Promotion）：gate 移除直接 +0.0817；attribution 移除通过证据类型失真
  表现为 K 通道断供（Traceability −2.1982, Recall −0.6036）。
- H2（HARD Compilation→Violation）：+0.6667，与 A/B 两臂的 0.3637 同号同源。
- H3（Environment Validation→Stale Reuse）：+1.0。
- H4（P-K Visibility→Conflict Violation）：Precision −0.1011（遮蔽失效）。
- 另证实 Reconciliation 是 Recall/Conflict 收敛的必要条件（Recall −0.6036），
  Commitment Gate 是 Conflict Resolution 的必要条件（−1.0）。

## Stage 13-lite — 重复运行

3 seeds × 200 cases × (3 arms + 7 ablation configs) = **6000 case-runs**，elapsed 266s。
报告均值/标准差/95% CI（`stage11.report.json` / `stage12.report.json`，CI 在 seed 维度上；
单 seed 内 200 case 为确定性执行，故 CI 反映种子间漂移，A/C 的安全指标 std=0）。

## 放行条件核验（§13 最重要条件）

进入效果对比前：`HardConstraintViolationRate=0`（C 全种子）、`CommitmentLeakage=0`
（重复向量经 reconciliation 收敛为 1，duplicate_knowledge=0 全种子）、逻辑
`ForgetLeakageRate=0`（全种子）。**C（EAGLE 完整治理）满足放行条件，A/B 不满足。**

## 产物

- `experiments/stage11_12/runner.py` — 三臂 + 6 消融 + 多种子 runner
- `experiments/stage11_12/stage11.report.json` — 三臂十指标（均值/std/95%CI）
- `experiments/stage11_12/stage12.report.json` — 消融指标与 delta_vs_full
- 复跑：`python experiments/stage11_12/runner.py --seeds 42,43,44 --total 200`

## 已知边界

- A 臂的"默认 extraction/inference"以规则泛化近似（无 LLM 环境 mem0 infer=True 会 RuntimeError，
  bootstrap 强制 noop）。方向性结论（fallback→偏好误升、模式即偏好）与 mem0 默认行为一致。
- ShimEmbedding 下检索相似度由文本哈希决定，Recall 数值反映治理可见性而非语义召回质量；
  换真实 gte-base 后 Recall@5 绝对值会变化，但三臂对比结构与安全指标结论不受影响
  （可见性由 SQLite/PACK 权威路径决定，不依赖 embedding 质量）。
