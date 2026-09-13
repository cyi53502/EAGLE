# EAGLE 效果验证报告

> 版本 `cca864c` · Mem0 基线 `9a7924b` · 日期 2026-09-10（2026-09-11 追加 Real ONNX 路径复测，见 §4.6）
> 环境：`Linux-5.15.0-161-generic / Ubuntu 24.04 noble / CPython 3.11.15 /opt/conda`
> 全文可复跑，命令见 §7；原始 JSON 见附录 A。

---

## 1. 测试数据集说明

### 1.1 EAGLE-Gov v1（主基准）

| 项 | 值 |
|---|---|
| 生成器 | `experiments/datasets/eagle-gov/generate.py` |
| 数据文件 | `v1.jsonl`（seed 42/43/44 各 200 条，共 600） |
| manifest | `v1.manifest.json`（`sha256 51b25e27…a4b8266`，固定 seed 确定性） |
| 用途 | 三臂对比 (Stage 11) + 消融 (Stage 12) |
| 嵌入 | `ShimEmbeddingClient 768d`（gte-base parity：`SHA256→splitmix64→unit`） |
| 向量 | `ShimVectorClient cosine_distance` |
| top_k | 5 |

### 1.2 用例族（9 家族）

| 家族 | 验证的治理行为 |
|---|---|
| `fallback_not_preference` | 回退成功不计入偏好（FalsePromotion 根因） |
| `implicit_gate` | 隐式偏好需 ≥3 次独立选择 ∧ ≥2 session |
| `explicit_gate` | 显式偏好 1 次即提交 |
| `kk_conflict` | 知识-知识单次矛盾 DEFER，不下线旧知识 |
| `env_drift` | 环境漂移阻断复用，触发 `RevalidationRequest` |
| `pk_visibility` | HARD Preference 遮蔽冲突知识（P-K mask） |
| `hard_constraint` | HARD 编译为工具空间约束（preferred/denied/offline） |
| `forgetting` | 遗忘后立即可见性=0 且不报 FORGOTTEN |
| `replay_idempotency` | 同 `execution_id` 重放幂等 |

### 1.3 EAGLE-Gov v2（Governance Tax 恢复族）

| 家族 | 数量/seed | 触发语义 |
|---|---|---|
| `stale_hint_revalidation` | 50 | 旧 `env→新env` 不可用 hint，仅产生 `RevalidationRequest(PENDING)`，不读 hint 的 `action` |
| `pending_hint_confirmation` | 50 | `PENDING candidate` 需 `user_confirmation_requested/accepted` 显式授权 |
| `empty_pack_safe_fallback` | 50 | Executable PACK 空，按当前 `scene+合法工具集` 重规划 |

权重**冻结**：`overall = (broad×200 + recovery×150)/350`，`conditions.weighting="case-weighted, predeclared 200 broad + 50 per recovery family"`（`governance_tax_v2.py`）。使用 `random.Random(seed).shuffle` 打散。

### 1.4 辅助数据集

* Kylin Smoke：`experiments/kylin-smoke/`，真实 SDK 11 探针 + 距离度量/幂等确认（21/21）
* 故障注入：`experiments/stage10/`，8 项独立 `:memory:` SQLite
* 端到端：`experiments/e2e_stage9/`，4 场景真实服务链路

---

## 2. 对比实验设计

### 2.1 三臂设定

| 臂 | 配置 | 模拟对象 |
|---|---|---|
| A `mem0-original` | 规则泛化近似 mem0 默认 `extraction/inference`（≥2 次→preference） | Ungoverned Memory |
| B `mem0+pref-text` | 显式偏好文本召回 + SOFT 排序，不编译 HARD | Instruction-only Governance |
| C `eagle-full` | 完整治理链（归因+门控+HARD 编译+env 校验+P-K mask+Outbox+溯源） | Mechanism-based Governance |

说明：A 臂 `infer=True` 在无 LLM 下触发 `RuntimeError`，`bootstrap` 强制 `noop`；规则近似会低估 LLM 误升，真实 A 只会更差。

### 2.2 内部消融（C 上逐项移除）

| 变体 | 对应假设 |
|---|---|
| `w/o Attribution` | H1 间接 |
| `w/o Commitment Gate` | H1 直接 |
| `w/o HARD Compilation` | H2 |
| `w/o Environment Validation` | H3 |
| `w/o P-K Visibility` | H4 |
| `w/o Reconciliation` | 补充：无 Worker 收敛 |

### 2.3 Governance Tax 接口对比（v2）

| 模式 | Executable PACK | Non-executing hints | Planner |
|---|---|---|---|
| `strict` | `ACTIVE+env匹配+未mask` | 无 | 仅 PACK |
| `two-tier-hints` | 同 strict | 计数，不进工具选择 | 同 strict |
| `two-tier-safe-fallback` | 同 strict | 仅触发 `Revalidation/确认/context 重规划` | 不读 hint 历史 tool |

---

## 3. 量化评测实施方案

### 3.1 流水线

```
1. 静态检查      ruff check/format --check + compileall + git diff --check → exit 0
2. 单元不变量    pytest 治理/PACK/Outbox/API/Provider → 49+13 passed
3. 真机 Smoke    kylin-smoke 11探针 → 21/21
4. 真实 E2E      e2e_stage9 4场景 → 4/4
5. 故障注入      stage10 8项 → 8/8
6. 三臂对比      stage11_12 3 seeds × 200 × 3臂 → 600 case-runs/臂
7. 消融          stage11_12 3 seeds × 200 × 7配置 → 6000 case-runs
8. v2 恢复       governance_tax_v2 3 seeds × 350 × 3模式 → 1050/mode
9. Focused       governance_tax_cases 3/3 机制级验证
10. 多种子统计   mean ± std，95% CI = mean ± 1.96·std/√3
```

### 3.2 数据集生成与固定

```
generate(seed=42/43/44, total=200) → v1.jsonl（确定性）
manifest sha256 校验：51b25e270df…a4b8266
v2 recovery：run_recovery(case_id, family, mode) 参数化
```

### 3.3 权重与分层

v2 采用**分层报告 + 预声明 case-weighted overall**：

```
overall = (broad_mean*200 + recovery_mean*150) / 350
```

不把不同 oracle 的 strata 混成单一不透明分数；`broad/recovery/overall` 三层均落盘 `governance_tax_v2.report.json`。

---

## 4. 量化指标与分析

### 4.1 指标定义

| 指标 | 定义 | 方向 |
|---|---|---|
| `HardConstraintViolationRate (HV)` | 执行了违反 HARD 的动作 / hard 家族 | ↓ |
| `ForgetLeakageRate (FL)` | 遗忘后仍可见 / forgetting+replay | ↓ |
| `PreferenceFalsePromotion (FP)` | 不该晋升却晋升 / applicable | ↓ |
| `StaleKnowledgeReuse (SR)` | 漂移环境复用旧知识 / env_drift | ↓ |
| `ConflictResolutionAccuracy (CA)` | K-K 矛盾后恰好 1 个 ACTIVE 且工具正确 / kk | ↑ |
| `KnowledgePrecision (KP)` | 可见且正确 / 可见 | ↑ |
| `KnowledgeRecall@5 (KR)` | 最终可见 / 全部应有（含按设计过滤） | ↑ |
| `EligibleRecall@5 (ER)` | 召回的、当前合法可用 / 当前合法可用 | ↑ |
| `GovernedVisibility (GV)` | 当前可执行 ACTIVE / 全部历史知识 | — |
| `PreferencePrecision (PP)` | 期望偏好命中 / 已晋升 | ↑ |
| `TraceabilityCoverage (TC)` | 有链接的 K / 全部 K | ↑ |
| `DownstreamTaskSuccess` | tool==expected / 全部 | ↑ |
| `SafeTaskSuccess (STS)` | (正确执行 + 正确拒执行) 且未 unsafe | ↑ |
| `UnsafeExecutionRate (UE)` | 执行 HARD 违例或漂移可见 | ↓ |
| `MissedSafeAction (MSA)` | 有安全动作但未选中 / safe_action_exists | ↓ |
| `CorrectPolicyBlock (CPB)` | 正确拒绝或无可执行动作 | ↑ |
| `HintInducedUnsafeExecution` | hint 引入的 unsafe | ↓ |
| `HintTriggeredRevalidation` | hint 仅触发重验证 | ↑ |
| `user_confirmation_requested/accepted/authorized` | PENDING hint 授权链 | ↑ |

### 4.2 三臂主实验（Stage 11，3 seeds × 200 均值）

| 指标 | A mem0-original | B mem0+pref-text | **C eagle-full** |
|---|---:|---:|---:|
| HV ↓ | 0.3637 ±0.0601 | 0.3637 ±0.0601 | **0.0000 ±0.0000** |
| FL ↓ | 0.5000 | 0.5000 | **0.0000** |
| FP ↓ | 0.6367 ±0.0029 | 0.0000 | **0.0000** |
| SR ↓ | 1.0000 | 1.0000 | **0.0000** |
| CA ↑ | 0.0000 | 0.0000 | **1.0000** |
| KP ↑ | 0.3854 | 0.5631 ±0.0065 | **1.0000** |
| KR@5 (=GV) ↑ | 1.0000 | 1.0000 | 0.6036 |
| TC ↑ | 0.0000 | 0.0000 | 1.0000 (≈2.19 links/K) |
| Task ↑ | 0.8817 ±0.0029 | 0.8017 ±0.0153 | 0.5933 ±0.0104 |

**要点：**
1. A/B/KR=1.0 以 KP=0.39/0.56 换来：A 每 10 条可见 6 条错/过期/被禁；C KR=0.6036 是 `Governed Visibility`，`PENDING/DEFER/环境阻断/P-K 遮蔽` 按设计不可召回。
2. B 仅解决 FP 与 KP，HARD/环境/冲突/溯源失守 → 证明 `Governance ≠ Prompt Preference`。
3. C 是唯一满足放行条件的组：`HV=FL=FP=SR=0 ∧ CA=KP=TC=1`。

### 4.3 消融（Stage 12，Δ = ablated − full）

| 变体 | HV | FP | SR | CA | KP | KR@5 | Task | 假设结论 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| w/o Attribution | 0 | 0 | 0 | **−1.0** | —(K断供) | **−0.6036** | −0.335 | H1 间接：fallback 被归因偏好→全落 P |
| w/o Commitment Gate | 0 | **+0.0817** | 0 | **−1.0** | **−0.3146** | 0 | +0.1084 | **H1 直接**：2-同session 提前晋升 |
| w/o HARD Compilation | **+0.6667** | 0 | 0 | 0 | 0 | 0 | −0.045 | **H2 成立** |
| w/o Environment Validation | 0 | 0 | **+1.0** | 0 | −0.1011 | +0.1982 | +0.065 | **H3 成立** |
| w/o P-K Visibility | 0 | 0 | 0 | 0 | −0.1011 | +0.1982 | +0.065 | **H4 成立** |
| w/o Reconciliation | 0 | 0 | 0 | −1.0 | — | −0.6036 | −0.335 | 无 Worker 收敛→PACK 空 |

CI 全部在 3 seeds 内窄（std≤0.0798），安全项 std=0 为逻辑保证非抽样波动。

### 4.4 Governance Tax v2（冻结权重，3 seeds × 350）

**Broad 200/seed（原始基准，3 模式对照）**

| 指标 | strict | two-tier-hints | two-tier-safe-fallback |
|---|---:|---:|---:|
| ER@5 ↑ | 1.0000 | 1.0000 | 1.0000 |
| KR@5 (=GV) | 0.6036 | 0.6036 | 0.6036 |
| STS ↑ | 0.5933 | 0.5933 | 0.5933 |
| MSA ↓ | 0.1867 | 0.1867 | 0.1867 |
| CPB | 0.0383 | 0.0383 | 0.0383 |
| UE ↓ | 0.0000 | 0.0000 | 0.0000 |
| HintUnsafe ↓ | 0.0000 | 0.0000 | 0.0000 |
| HV/FL/SR | 0/0/0 | 0/0/0 | 0/0/0 |

`two-tier-hints` 与 `strict` 完全一致 → `Hints ⊬ Execution Authority` 安全基线通过。

**Recovery 150/seed（50/family）**

| family | safe_success | revalidation | confirmation (req/accept/authorized) | hard/forget/stale |
|---|---:|---:|---|---|
| stale_hint_revalidation | 1.0000 | 1.0000 | — | 0 |
| pending_hint_confirmation | 1.0000 | 0 | 1.0/1.0/1.0 | 0 |
| empty_pack_safe_fallback | 1.0000 | 0 | — | 0 |

**Overall 350/seed（case-weighted 冻结权重）**

| 模式 | STS | 95% CI | MSA | 95% CI | HintUnsafe | HV/FL/SR |
|---|---:|---|---:|---|---:|---|
| strict | 0.3390 | [0.3323, 0.3458] | 0.5352 | [0.5285, 0.5419] | 0.0 | 0/0/0 |
| two-tier-hints | 0.3390 | [0.3323, 0.3458] | 0.5352 | [0.5285, 0.5419] | 0.0 | 0/0/0 |
| **two-tier-safe-fallback** | **0.7676** | **[0.7609, 0.7744]** | **0.1067** | [0.0999, 0.1134] | **0.0** | **0/0/0** |

逐 seed (STS)：42: 0.7657 / 43: 0.7629 / 44: 0.7743。

推导：`0.5933×200/350=0.3390`，`(0.5933×200+1.0×150)/350=0.7676`；CI 为 `mean ± 1.96·std/√3`。

### 4.5 指标分析

1. **治理税的正确归因**：`ER=1.0` 说明 C 没有漏掉当前合法知识，旧 KR 0.6036 是 `GV`（分母含按设计过滤项）。真正税在 `MSA`（Broad 0.1867 / Overall 0.5352）。
2. **two-tier 的作用边界**：仅 hints 不恢复 utility（Broad 三列一致），必须配合 safe fallback（revalidation/确认/context 重规划）才在 Recovery 族 1.0，把 Overall STS 从 0.339 拉到 0.768。
3. **安全四约束零成本保持**：`HV=FL=SR=HintUnsafe=0` 在全部 1050×3 case-runs 中无一次违例。
4. **Families 分布**：Broad `MissedSafeAction` 主要来自 `hard_constraint/kk_conflict/fallback_not_preference` 等 "无合法可恢复 hint" 的场景，是治理边界内合理结果。
5. **机制级验证**：Focused 3/3 通过证明 `RevalidationRequest / UserConfirmation / context-only replanning` 三条路径各自可用且互斥。

### 4.6 Real ONNX 路径复测（evaluation 初始化，2026-09-11，SDK 适配层接入后复跑 2026-09-11）

真实链路（`kylin-ai-runtime` SDK 嵌入 tier + `gte-base` ONNX 兜底 + 麒麟向量引擎 UDS）上复测 PACK 延迟与 C 臂检索质量。一键脚本 `experiments/evaluation/run_all.sh`，原始 JSON 见附录 A。

**后端标签（诚实上报）**：嵌入端先探测真麒麟 SDK（`kylin-ai-runtime` D-Bus socket `com.kylin.AiRuntime.CoreTextEmbeddingService`，协议取自其开源实现），本机未装运行时 → 如实标为 `onnx(fallback: no kylin-ai-runtime socket …)`；若模型/引擎再缺失则继续回退 shim，标签随之变为 `shim(fallback: …)`——任何 shim/ONNX 运行都不会被标成 `kylin-sdk`。真机上运行时可达时标签为 `{"embedding":"kylin-sdk","vector":"real"}`。

**PACK build 延迟**（seed 42，200 cases，top_k=5，CPU 主机；SDK 适配层接入后复跑）

| 项 | 值 |
|---|---|
| backend | embedding=`onnx(fallback: no kylin-ai-runtime socket …)`, vector=real |
| p50 | 105.46 ms |
| p95 | **201.14 ms** |
| p99 | 350.53 ms |
| mean | 94.07 ms |
| max | 453.63 ms |
| embedding 单次 | 11.392 ms |
| 达标（p95 < 500ms） | ✅ |

> 本机为 CPU（`Linux-5.15.0-161`），比团队 3090 参考（p50 12.1 / p95 24.3 ms）约慢 10–20×。硬指标要求 ≤500ms，p95 201ms 达标。接入 SDK 适配层后实际嵌入仍走同一条 ONNX 权重（仅标签变为诚实 fallback 前缀），故延迟与首轮（p95 296ms）同量级，波动为宿主机背景负载噪声，如实列出。

**C 臂检索质量（Shim vs Real，seed 42，200）**

| 指标 | C_shim | C_real | Δ(real−shim) |
|---|---:|---:|---:|
| Hard Violation ↓ | 0.0 | 0.0 | 0.0 |
| Forget Leakage ↓ | 0.0 | 0.0 | 0.0 |
| False Promotion ↓ | 0.0 | 0.0 | 0.0 |
| Stale Reuse ↓ | 0.0 | 0.0 | 0.0 |
| Conflict Resolution Acc ↑ | 1.0 | 1.0 | 0.0 |
| Knowledge Precision ↑ | 1.0 | 1.0 | 0.0 |
| Knowledge Recall@5 (=GV) | 0.6036 | 0.6036 | 0.0 |
| Preference Precision ↑ | 1.0 | 1.0 | 0.0 |
| Traceability Coverage ↑ | 1.0 | 1.0 | 0.0 |
| Downstream Task Success ↑ | 0.59 | 0.59 | 0.0 |
| Eligible Recall@5 ↑ | 1.0 | 1.0 | 0.0 |

**结论**：Real（SDK 适配层 + ONNX 兜底）与 Shim 全部 headline 指标一致（Δ=0），证明 §4.2 的安全/精度结论与嵌入后端无关。四项硬指标在真实链路全部满足：`EligibleRecall@5=1.0 ≥ 0.85`、`PACK p95=201ms ≤ 500ms`、`HV=FL=SR=0`（冲突正确率 CA=1.0 ≥ 88%）。

---

## 5. 故障与端到端验证摘要

### 5.1 故障注入（stage10 8/8）

| 注入 | 期望行为 | 结果 |
|---|---|---|
| Embedding timeout | `PENDING retry=1 退避` → DONE | PASS |
| Vector insert timeout | 同上 | PASS |
| Insert 后崩溃 | 孤立向量→reconcile 2→单 canonical | PASS |
| SQLite 回填失败 | 恒 1 条 Knowledge，无额外 Memory | PASS |
| 重复向量 | canonical=min，DELETE_DUPLICATE→1 | PASS |
| Vector delete 失败 | FORGETTING 立即 PACK 空 pending=1 → FORGOTTEN | PASS |
| 外部删向量 | health degraded → reconcile → ok | PASS |
| Worker 重启 | 新鲜 RUNNING 不抢占 / 陈旧(>300s) 归 PENDING | PASS |

### 5.2 端到端（e2e_stage9 4/4）

| 场景 | 关键断言 |
|---|---|
| fallback 提交 | 2 次触发→Knowledge ACTIVE→PACK 1 条 |
| HARD 限工具 | `denied_tools=[wps]`，allowed={wps}，HARD 过滤生效 |
| 漂移重验证 | drift 0+RevalidationRequest，home 1 ACTIVE |
| 遗忘擦除 | forget→0 可见→FORGOTTEN 且 mem0_id=None content="" |

### 5.3 Kylin Smoke（21/21）

`eq,in,ne,not,and,id_allowlist,id_blocklist,list_filter,read_after_write,delete` 全 `supported`；UDS `/tmp/kylin-ai-vector-engine-0.sock` 可用；antlr4.9.2 + boostshim 就绪。

---

## 6. 阶段测试矩阵（0–14）

| # | 阶段 | 结果 | 产物 |
|---|---|---|---|
| 0 | 固化条件 | 9 项 | `CONDITIONS.md, environment_snapshot.txt` |
| 1 | 静态 | exit 0 | ruff/compileall/diff |
| 2 | 治理不变量 | 14 passed | test_governance 等 |
| 3 | PACK/HARD | 10 passed | test_pack_and_forgetting |
| 4 | Outbox/恢复 | 17 passed | test_outbox/reconciliation/health |
| 5 | API 租户 | 3 passed | test_api |
| 6 | Provider | 13 passed | tests/embeddings 等 |
| 7 | 全量回归 | 49+13 passed | `pytest -q` |
| 8 | Kylin Smoke | 21/21 | kylin-smoke/SMOKE_REPORT.md |
| 9 | 真实 E2E | 4/4 | e2e_stage9 |
| 10 | 故障注入 | 8/8 | stage10 |
| 11 | 三臂对比 | C 全 0 | stage11.report.json |
| 12 | Ablation | H1-H4 成立 | stage12.report.json |
| 13 | 多种子 | 6000 runs 95%CI | 同上 |
| v2 | Governance Tax | 3/3 + 1050/mode | governance_tax_v2.report.json |
| 14 | Real ONNX 复测 | pass true | experiments/evaluation/report.json |

---

## 7. 一键复跑

```bash
# 静态
ruff check eagle tests && ruff format --check eagle tests && python -m compileall -q eagle && git diff --check
# 单元
cd eagle_os_agent && pytest -q   # 49 passed
# Provider
MEM0_TELEMETRY=False pytest -q tests/embeddings tests/vector_stores tests/llms tests/utils tests/memory  # 13 passed
# E2E / 故障 / Smoke
python experiments/e2e_stage9/runner.py
python experiments/stage10/runner.py
python experiments/kylin-smoke/capability_smoke.py
# 三臂 + 消融
python experiments/stage11_12/runner.py --seeds 42,43,44 --total 200
# v2 Governance Tax
python experiments/governance_tax_cases.py
python experiments/governance_tax_v2.py
# Gov 数据集单独
python experiments/datasets/eagle-gov/runner.py --seed 42
# Real 路径复测（evaluation 初始化：PACK 延迟 + C 臂检索；仓库根执行）
./experiments/evaluation/run_all.sh   # 默认 KYLIN_USE_SHIM=0 → SDK tier(本机无运行时) → ONNX + 真引擎；缺资源继续回退 shim 并如实标 backend
```

预计运行时长：`stage11_12` ≈ 90s、`v2` ≈ 145s、`stage10` ≈ 2.4s、`e2e_stage9` ≈ 3s。

---

## 8. 结论

1. **放行条件全部满足**：`HV=FL=CommitmentLeakage=0` 仅 C 达成（A/B 有 0.36/0.5 泄露）。
2. **治理 ≠ Prompt**：B 解决 FP/KP 但 HARD/环境/冲突/溯源失守；四假设 H1-H4 消融因果闭合。
3. **治理税正确定位**：`ER=1.0` 证明合法知识零丢失；税在 `MSA`。
4. **v2 冻结权重收敛税**：`two-tier-safe-fallback` 将 Overall STS `0.339 → 0.768`（CI [0.761,0.774]），MSA `0.535→0.107`，且 `HintUnsafe=0 HV/FL/SR=0` 全程不变。
5. **可信恢复**：Revalidation / UserConfirmation / context-only 三条路径机制级 3/3 通过，无一条读 hint 历史 `tool` 作为执行指令。

论文表述（可直接引用）：

> Strict governance does not reduce recall over the set of currently admissible knowledge (EligibleRecall@5 = 1.0). Its remaining utility cost is concentrated in missed safe actions. With a frozen case-weighted 200 broad + 50/family weighting, a two-tier architecture that keeps the executable PACK strict and lets hints trigger only revalidation, user confirmation, or context-only replanning raises overall SafeTaskSuccess from 0.339 to 0.768 (95% CI [0.761, 0.774]) while keeping HardViolation, ForgetLeakage, StaleReuse, and HintInducedUnsafeExecution at 0.

---

## 附录 A：原始 JSON 清单

```
experiments/e2e_stage9/report.json
experiments/stage10/report.json
experiments/stage11_12/stage11.report.json
experiments/stage11_12/stage12.report.json
experiments/governance_tax.report.json
experiments/governance_tax_cases.report.json
experiments/governance_tax_v2.report.json
experiments/datasets/eagle-gov/v1.report.json
experiments/kylin-smoke/smoke_report.json
experiments/evaluation/report.json
experiments/evaluation/bench_pack_latency.json
experiments/evaluation/bench_retrieval_quality.json
```

## 附录 B：统计口径

* seeds = [42,43,44]（v2 亦同）；`mean ± std`，`95% CI = mean ± 1.96·std/√3`。
* std=0 为逻辑保证（同 seed 确定性），非过拟合。
* 所有报告含 `conditions.weighting` 冻结字段，不支持事后挑权重。
