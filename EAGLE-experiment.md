按“低成本正确性 → 外部依赖 → 故障安全 → 效果对比”的顺序跑，任何阶段失败都不要进入下一阶段。

## 0. 固化实验条件

先记录：

```
Git commit
Python 与依赖版本
Mem0 commit
麒麟 SDK/服务版本
Embedding 模型及维度
Vector distance_metric
score_semantics
随机种子
数据集版本
```

正式实验不能使用之前测试用的 import stub。

## 1. 静态检查

```
cd /root/rivermind-data/mem0/eagle_os_agent

ruff check eagle tests
ruff format --check eagle tests
python -m compileall -q eagle
git -C .. diff --check
```

成功标准：全部退出码为 0。

## 2. 核心治理不变量

```
pytest -q \
  tests/test_governance.py \
  tests/test_preference_resolver.py \
  tests/test_knowledge_revalidation.py
```

优先验证：

```
Fallback 不晋升 Preference
重复 execution_id 不重复计证据
显式 Preference 一次提交
隐式 Preference 满足 3 次、2 个 session
用户纠正产生负证据
PENDING 不可召回
K-K 单次矛盾不立即下线旧知识
环境版本形成正确 lineage
```

## 3. PACK 与 HARD 安全

```
pytest -q tests/test_pack_and_forgetting.py
```

必须满足：

```
HARD Preference 物理过滤工具
无合法工具返回 NO_FEASIBLE_ACTION
不可执行的 HARD key 快速失败
PACK 只返回 SQLite ACTIVE Knowledge
环境不匹配不返回
回到原环境仍能使用原知识
P-K mask 生效且可逆
重复向量不导致重复 Knowledge
```

这一阶段主要对应：

```
HardConstraintViolationRate = 0
CommitmentLeakage = 0
```

## 4. Outbox、遗忘和恢复

```
pytest -q \
  tests/test_outbox.py \
  tests/test_reconciliation.py \
  tests/test_health.py
```

注入以下故障：

```
Mem0 add 成功、SQLite 回填前崩溃
向量重复
向量丢失
delete 失败
Worker 超出租约
Worker 租约未过期
FAILED job
FORGETTING 尚未物理删除
```

成功标准：

```
不产生额外正式 Memory
重复向量最终收敛为一个
新鲜 RUNNING job 不被抢占
FORGETTING 立即不可召回
删除失败不报告 FORGOTTEN
物理删除后内容和孤立证据被清除
/health 正确进入 degraded
```

## 5. API 与租户隔离

```
pytest -q tests/test_api.py
```

验证：

```
无认证请求 → 401
payload 不能指定 user_id
跨用户读取 → 空结果或 404
跨用户 forget/revoke → 404
相同 execution_id 重放 → 原结果
相同 execution_id 不同内容 → 409
不完整 fallback Evidence → 422
```

## 6. Mem0 Provider 契约测试

在仓库根目录运行：

```
cd /root/rivermind-data/mem0

MEM0_TELEMETRY=False pytest -q \
  tests/embeddings/test_kylin_embedding.py \
  tests/vector_stores/test_kylin_vector_store.py \
  tests/llms/test_noop.py \
  tests/utils/test_factory.py \
  tests/memory/test_kylin_raw_memory.py
```

必须明确断言：

```
kylin/noop Factory 构造正确
infer=False 不调用 LLM
每条 raw memory 只 embedding 一次
维度不一致立即失败
Vector score 越大越相似
add/search/delete 闭环成立
配置错误不 fallback 到默认 Provider
```

## 7. 全量本地回归

```
cd /root/rivermind-data/mem0/eagle_os_agent
pytest -q
```

当前基线：

```
49 passed
```

Provider 当前基线：

```
13 passed
```

## [ 8. 真实麒麟 SDK Smoke Test](SDK接入)

接入真实 SDK 后，先使用临时 collection 验证：

```
创建 collection
写入 2 个向量
按 ID 读取
EQ filter
IN filter
NOT filter
AND filter
ID allowlist
ID blocklist
list + filter
向量搜索
删除向量
删除 collection
```

重点确认：

```
真实 distance_metric 枚举
SDK 返回的是 similarity 还是 distance
metadata filter 语法
写入后可见性延迟
ID 类型
批量限制
delete 幂等语义
```

能力探测不通过时不得启动正式服务。

## 9. 真实端到端闭环

使用真实麒麟服务执行：

```
Episode
→ Attribution
→ Candidate
→ Gate
→ SQLite COMMITTED
→ IndexJob
→ Worker
→ Mem0.add(infer=False)
→ KylinEmbedding
→ KylinVectorStore
→ PACK 检索
```

至少覆盖四个场景：

1. 两次相同 fallback 后提交 Knowledge。
2. 显式 HARD Preference 立即限制工具。
3. 环境切换生成 Revalidation Request，原环境知识仍可用。
4. Forget 后立即不可召回，Worker 完成后向量及内容消失。

## 10. 故障注入实验

在真实服务上分别注入：

```
Embedding timeout
Vector insert timeout
Vector insert 成功后进程退出
SQLite 回填失败
重复向量
Vector delete 失败
向量被外部删除
Worker 执行中重启
```

观测：

```
IndexJob 状态
retry_count
available_at
Knowledge 状态
PACK 可见性
/health
reconciliation 收敛时间
```

## 11. 三组主实验

保持数据、Embedding、VectorStore、top_k 完全一致：

|实验组|方案|
|---|---|
|A|Mem0 original，默认 extraction/inference|
|B|Mem0 + 简单 Preference 文本召回|
|C|Mem0 + EAGLE 完整治理|

主要指标：

```
Preference Precision
Preference False Promotion Rate
Knowledge Precision
Knowledge Recall@K
Hard Constraint Violation Rate
Stale Knowledge Reuse Rate
Conflict Resolution Accuracy
Forget Leakage Rate
Traceability Coverage
Downstream Task Success
```

安全指标优先级高于向量 Recall。

## 12. Ablation 实验

从完整 EAGLE 逐项移除：

```
EAGLE - Attribution
EAGLE - Commitment Gate
EAGLE - HARD Compilation
EAGLE - Environment Validation
EAGLE - P-K Visibility
EAGLE - Reconciliation
```

用于分别验证 H1–H5，尤其观察：

```
去掉 Attribution
→ False Promotion 是否上升

去掉 HARD Compilation
→ Constraint Violation 是否上升

去掉 Environment Validation
→ Stale Knowledge Reuse 是否上升

去掉 P-K Visibility
→ P-K Conflict Violation 是否上升
```

## 13. 重复运行与最终报告

每组至少使用多个随机种子重复运行，报告：

```
均值
标准差
95% 置信区间
失败样例
各状态转移数量
```

推荐最终流水线：

```
静态检查
→ 单元不变量
→ PACK 安全
→ Outbox/故障恢复
→ API 租户隔离
→ Mem0 Provider
→ 全量回归
→ 麒麟 Smoke
→ 真实 E2E
→ 故障注入
→ Baseline 对比
→ Ablation
→ 长时间稳定性
```

最重要的放行条件是：在开始效果指标比较前，`HardConstraintViolationRate`、`CommitmentLeakage` 和逻辑 `ForgetLeakageRate` 必须先达到 0。