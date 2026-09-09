# EAGLE-Gov v1 — 本地治理数据集

不变量对照 `EAGLE-experiment.md`。每个 case 按归因、门控、约束、冲突、环境、遗忘与重放之一靶向测试。生成方式确定性（seed=42），写入的 `episodes[].scene` 是唯一的归因 scene。

```
SEED=42 total=200
sha256: 51b25e270df1309aa57cd4683a9425a3ed533e3b71d0b1a8092d61380a4b8266
```

| Family | 数量 | 靶点 |
|---|---|---|
| `fallback_not_preference` | 23 | Fallback 成功绝不产生 Preference |
| `implicit_gate` | 23 | 3 次跨 2 session 晋升；correction 撤销；变体 `REJECTED` |
| `explicit_gate` | 22 | HARD/SOFT 单次提交 |
| `hard_constraint` | 22 | HARD 工具过滤 / `NO_FEASIBLE_ACTION` / 拒绝 `allowed_formats`/`privacy_rule` |
| `kk_conflict` | 22 | 首次同条件矛盾 `DEFER` 不下线旧知识 |
| `env_drift` | 22 | PACK 在漂移环境阻挡并写入 `knowledge_revalidations`；原环境仍可用 |
| `pk_visibility` | 22 | `ConflictService.mask_knowledge` 使 HARD P 遮蔽 K |
| `forgetting` | 22 | 遗忘后 PACK 不可见且同用户作用域 |
| `replay_idempotency` | 22 | 相同 `execution_id` 重放不新增 evidence/job |
