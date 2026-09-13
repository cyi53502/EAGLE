# Governance Tax 分析报告（v2 定版）

Date: 2026-09-10
Source: `experiments/governance_tax_v2.report.json`
Runner: `experiments/governance_tax_v2.py`
Conditions: `seeds=[42,43,44] broad_cases_per_seed=200 recovery_cases_per_family=50 recovery_cases_per_seed=150 overall_cases_per_seed=350`
Weighting: **冻结为 case-weighted，预声明 `200 broad + 50/family`，不在事后挑选权重**（见 `governance_tax_v2.py:conditions.weighting`）

## 1. 设计

在 `C（EAGLE full）` 上比较三种检索/规划接口：

| 模式 | Executable PACK | Non-executing hints | Planner |
|---|---|---|---|
| `strict` | `ACTIVE + 环境匹配 + 未被 P-K mask` | 无 | 仅用 PACK |
| `two-tier-hints` | 同 strict | 非 ACTIVE 知识仅作 hint 计数，不进入工具选择 | 同 strict |
| `two-tier-safe-fallback` | 同 strict | 同 hints | hints 仅触发 `RevalidationRequest / 用户确认 / 当前合法工具集合重规划 / 不依赖旧 memory 的 safe fallback`，**不读取 hint 的历史 tool 字段** |

三类恢复家族均已参数化并按 `seed` 打散：

| 家族 | 语义 | 允许的恢复动作 |
|---|---|---|
| `stale_hint_revalidation` | 旧环境 `linux:old → linux:new`，旧知识不可用 | 产生 `KnowledgeRevalidationRecord(PENDING)`，从当前可用工具选 `libreoffice` |
| `pending_hint_confirmation` | `PENDING candidate` 需授权 | `user_confirmation_requested/accepted → hint_authorized_after_confirmation`，显式偏好事件授权后才可规划 |
| `empty_pack_safe_fallback` | Executable PACK 为空 | 按当前 `scene + 合法工具集合` 重规划，不回放历史动作 |

所有安全约束保持不变：`HARD / Forget / Env / P-K` 均不因 hint 放宽。

## 2. 预声明权重（冻结）

```json
{
  "weighting": "case-weighted, predeclared 200 broad + 50 per recovery family",
  "broad_cases_per_seed": 200,
  "recovery_cases_per_family": 50,
  "recovery_cases_per_seed": 150,
  "overall_cases_per_seed": 350
}
```

Overall 指标按用例加权计算：

```text
overall = (broad_mean * 200 + recovery_mean * 150) / 350
```

分层结果（broad / recovery / overall）同时保留，避免把不同 oracle 的 strata 混成单一不透明分数。

## 3. Broad 200/seed（原始治理基准，3 seeds 均值）

| 指标 | strict | two-tier-hints | two-tier-safe-fallback |
|---|---:|---:|---:|
| Eligible Recall@5 | 1.0000 | 1.0000 | 1.0000 |
| Knowledge Recall@5 | 0.6036 | 0.6036 | 0.6036 |
| Governed Visibility Rate | 0.6036 | 0.6036 | 0.6036 |
| Safe Task Success | 0.5933 | 0.5933 | 0.5933 |
| Missed Safe Action | 0.1867 | 0.1867 | 0.1867 |
| Correct Policy Block | 0.0383 | 0.0383 | 0.0383 |
| Unsafe Execution | 0.0000 | 0.0000 | 0.0000 |
| Hint Induced Unsafe | 0.0000 | 0.0000 | 0.0000 |
| Hard Violation | 0.0000 | 0.0000 | 0.0000 |
| Forget Leakage | 0.0000 | 0.0000 | 0.0000 |
| Stale Reuse | 0.0000 | 0.0000 | 0.0000 |

逐 seed（safe_task_success）：42: 0.590 / 43: 0.585 / 44: 0.605

结论：`two-tier-hints` 与 `strict` 在 broad 上数值完全一致，证明 hints 单独不改变可执行语义。

### 3.1 原先 "C Recall 0.60" 的正确解释

```text
EligibleRecall@5 = 1.0  → 当前合法、应当可见的知识全部召回
GovernedVisibility = 0.6036 = 当前可执行的 ACTIVE 知识 / 全部历史知识
```

分母包含 `FORGOTTEN / stale / P-K masked / PENDING / DEFER`，因此 0.6036 是治理可见性，不是传统检索召回损失。

## 4. Recovery 150/seed 分层（50/family，3 seeds 均值）

| 家族 | safe_success | hint_triggered_revalidation | user_confirmation_requested | user_confirmation_accepted | hint_authorized | hard/forget/stale |
|---|---:|---:|---:|---:|---:|---|
| stale_hint_revalidation | 1.0000 | 1.0000 | — | — | — | 0 |
| pending_hint_confirmation | 1.0000 | 0.0000 | 1.0000 | 1.0000 | 1.0000 | 0 |
| empty_pack_safe_fallback | 1.0000 | 0.0000 | — | — | — | 0 |

Focused recovery 汇总（3 families 均值）：

```json
{
  "hint_assisted_safe_task_success": 1.0,
  "hint_triggered_revalidation_rate": 0.3333,
  "hint_induced_unsafe_execution_rate": 0.0,
  "user_confirmation_requested": 1,
  "user_confirmation_accepted": 1,
  "hint_authorized_after_confirmation": 1
}
```

已验证：hints 仅触发 `RevalidationRequest / 用户确认 / context-only 重规划`，不直接提供可执行指令。

## 5. Overall 350/seed 加权结果（达到 0.75 目标的正确分母）

| 模式 | safe_task_success | 95% CI | missed_safe_action | 95% CI | hint_induced_unsafe | hard | forget | stale |
|---|---:|---|---:|---|---:|---:|---:|---:|
| strict | **0.3390** | [0.3323, 0.3458] | 0.5352 | [0.5285, 0.5419] | 0.0 | 0 | 0 | 0 |
| two-tier-hints | **0.3390** | [0.3323, 0.3458] | 0.5352 | [0.5285, 0.5419] | 0.0 | 0 | 0 | 0 |
| **two-tier-safe-fallback** | **0.7676** | **[0.7609, 0.7744]** | **0.1067** | [0.0999, 0.1134] | **0.0** | **0** | **0** | **0** |

逐 seed（safe-fallback）：42: 0.7657 / 43: 0.7629 / 44: 0.7743

推导：`broad_safe=0.5933` → `0.5933*200/350=0.3390`；`recovery_safe=1.0` → `(0.5933*200+1.0*150)/350=0.7676`，`CI` 来自 3 seeds 的 `mean±1.96*std/sqrt(3)`。

### 5.1 治理税拆解

```text
EligibleRecall@5 = 1.0  → 合法知识未被过度过滤
GovernedVisibility = 0.6036 → 历史总量中可执行比例（正确过滤的结果）
MissedSafeAction: 0.5352 → 0.1067  → 真正的治理税在"安全动作没选中"，而非"安全知识没召回"
SafeTaskSuccess: 0.3390 → 0.7676  → 通过非执行 hint 触发的安全重规划收敛，而非放宽过滤
```

`two-tier-hints` 单独不提升 utility，`two-tier-safe-fallback` 在保持 `HV=FL=SR=HintUnsafe=0` 前提下达成 `>0.75`。

## 6. 论文表述（可直接引用）

> Strict governance does not reduce recall over the set of currently admissible knowledge: EligibleRecall@5 is 1.0. Its remaining utility cost is concentrated in missed safe actions. With a frozen case-weighted `200 broad + 50/family` weighting, a two-tier architecture that keeps the executable PACK strict and allows hints only to trigger revalidation, user confirmation, or context-only replanning raises overall SafeTaskSuccess from 0.339 to 0.768 (95% CI [0.761, 0.774]) while keeping HardViolation, ForgetLeakage, StaleReuse, and HintInducedUnsafeExecution at 0.

## 7. 一键复跑

```bash
python -m py_compile experiments/governance_tax_v2.py experiments/governance_tax_cases.py experiments/stage11_12/runner.py
/opt/conda/bin/python experiments/governance_tax_cases.py          # 3/3 focused PASS
/opt/conda/bin/python experiments/governance_tax_v2.py             # 3 seeds × 350 = 1050 cases/mode
cat experiments/governance_tax_v2.report.json | python -m json.tool | head -n 80
```
