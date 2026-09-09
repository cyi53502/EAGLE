# Gov Harness Report — 2026-09-09

按 `EAGLE-experiment.md` 顺序执行的全部阶段结果。执行入口均在
`/root/rivermind-data/EAGLE/eagle_os_agent`（conda base Python 3.11.15）。

## 1. 静态检查（阶段 1）

| 命令 | 结果 |
|---|---|
| `ruff check eagle tests` | All checks passed（exit 0） |
| `ruff check eagle_os_agent evaluation` | All checks passed（exit 0） |
| `ruff format --check eagle_os_agent evaluation` | 59 files already formatted |
| `python -m compileall -q eagle` | exit 0 |
| `git diff --check` | exit 0 |

## 2. 核心治理不变量（阶段 2）

```
pytest -q tests/test_governance.py tests/test_preference_resolver.py tests/test_knowledge_revalidation.py
=> 14 passed
```

## 3. PACK 与 HARD 安全（阶段 3）

```
pytest -q tests/test_pack_and_forgetting.py
=> 10 passed
```

## 4. Outbox、遗忘和恢复（阶段 4）

```
pytest -q tests/test_outbox.py tests/test_reconciliation.py tests/test_health.py
=> 17 passed
```

## 5. API 与租户隔离（阶段 5）

```
pytest -q tests/test_api.py
=> 3 passed
```

## 6. Mem0 Provider 契约（阶段 6）

```
cd /root/rivermind-data/EAGLE
MEM0_TELEMETRY=False pytest -q tests/embeddings/test_kylin_embedding.py \
  tests/vector_stores/test_kylin_vector_store.py tests/llms/test_noop.py \
  tests/utils/test_factory.py tests/memory/test_kylin_raw_memory.py
=> 13 passed   （基线 13 passed 复现）
```

## 7. 全量本地回归（阶段 7）

```
pytest -q
=> 49 passed   （基线 49 passed 复现）
```

## 8. EAGLE-Gov v1 本地闭环（阶段 9 前置）

```
python experiments/datasets/eagle-gov/generate.py --seed 42 --total 200
=> sha256 51b25e270df1309aa57cd4683a9425a3ed533e3b71d0b1a8092d61380a4b8266

cd eagle_os_agent && PYTHONPATH=$PWD python ../experiments/datasets/eagle-gov/runner.py --seed 42
=> cases_total 200 / cases_passed 200 / case_pass_rate 1.0
```

| Family | total | passed |
|---|---|---|
| fallback_not_preference | 23 | 23 |
| implicit_gate | 23 | 23 |
| explicit_gate | 22 | 22 |
| hard_constraint | 22 | 22 |
| kk_conflict | 22 | 22 |
| env_drift | 22 | 22 |
| pk_visibility | 22 | 22 |
| forgetting | 22 | 22 |
| replay_idempotency | 22 | 22 |

安全不变量对应观测：HardConstraintViolationRate=0（I1/I4 全过）、CommitmentLeakage=0（I2/I3 全过）、逻辑 ForgetLeakageRate=0（I6 全过）。
因此已满足 §13 的效果指标比较放行条件中"本地可验证"部分。

## 9. 遗留

- 阶段 8 麒麟 Smoke 前需 `dpkg --configure -a` 配置 `kylin-ai-vector-engine 1.2.0.1-1+b2`，再跑能力探测。
- 阶段 8 通过后回填 `CONDITIONS.md` §5/§6 的 embedding 维度与 distance_metric/score_semantics 实测值。
- LongMemEval / LongMemEval-V2 / LoCoMo / OSWorld 数据集在阶段 8 后拉取登记。
