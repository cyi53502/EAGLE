# Stage 10 — 故障注入报告

Date: 2026-09-09 16:44 (+08) · Host: Linux-5.15.0-161-generic-x86_64 (Ubuntu 24.04 noble)
Runner: `experiments/stage10/runner.py` · Engine: ShimEmbedding 768d + ShimVector cosine_distance
（与真实 kylin-ai-model-service + kylin-ai-vector-engine UDS 同 Protocol）

注入（EAGLE-experiment.md §10）各独立 `:memory:` SQLite：

| # | 注入 | 观测 | 结论 |
|---|---|---|---|
| 1 | Embedding timeout | IndexJob PENDING retry=1 available_at 指退避，Knowledge ACTIVE mem0_id=None，`/health.pending=1`，重试后 DONE/mem0_id 非空 | Outbox 指退避，未丢失正式 Memory |
| 2 | Vector insert timeout | 同1，probe collection 的 upsert 隔离不计入注入计数 | 向量层超时可重试收敛 |
| 3 | Vector insert 成功后进程退出（SQLite 回填前崩溃） | 向量孤立，SQLite 仍无 mem0_id，Reconciliation 检测到 drift 变更 2，Worker 重跑后单一 canonical 向量 | 崩溃不泄露额外 Memory，最终收敛为 1 |
| 4 | SQLite 回填失败 | upsert 成功后写回抛异常，retry 不新增 Knowledge（恒 1），最终 DONE/mem0_id 存在 | 原子性由 Outbox 保证 |
| 5 | 重复向量 | 同 index_key 写 vec-a/vec-b，Worker 选 lexicographically smallest canonical，DELETE_DUPLICATE 调度后清理为 1 | 重复向量收敛 |
| 6 | Vector delete 失败 | DELETE PENDING retry=1，Knowledge=FORGETTING 但 PACK 已空，`pending=1`，放开后 FORGOTTEN/mem0_id=None/向量缺失 | FORGETTING 立即不可召回，失败不报 FORGOTTEN |
| 7 | 向量被外部删除 | `/health reconciliation_required=True degraded`，Reconciliation 清空 mem0_id 变更 2 并重排 UPSERT，最终健康 | 外部漂移被巡检修复 |
| 8 | Worker 执行中重启 | 新鲜 RUNNING (now) 不被抢占；陈旧 RUNNING (-600s) 归 PENDING（租约 300s） | 租约保护不重复执行 |

结果：**8/8 PASS** `report.json`。
复跑：`python experiments/stage10/runner.py`

已知：Health 的 degraded 仅在 FAILED/reconciliation_required 时触发，单纯 PENDING + 指数退避
不会置为 degraded（与 `tests/test_health.py` 一致），故 1 检验 pending_count 而非 status。
