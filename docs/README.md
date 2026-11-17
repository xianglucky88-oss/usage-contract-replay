# 计费重放维护交接文档（索引）

本目录是 SaaS 计费团队向维护人员交接的**约定文档**，描述目标行为（目标 schema 标记为
`2.0-draft`，样例位于 `fixtures/examples/`），并如实标注当前代码（`0.1.0`，扁平 v1 schema）
已经证明的能力与仍需开发的缺口。本次交接**不重新实现计费**，所有缺口只登记、不改代码。

| 文档 | 内容 | 对应交付物 |
| --- | --- | --- |
| [01_data_dictionary.md](01_data_dictionary.md) | 合同/用量数据字典：原始事件字段 vs 派生金额字段，v1 与 v2-draft 对照 | 数据字典 |
| [02_billing_period_rules.md](02_billing_period_rules.md) | 账期规则登记册 R1–R12，含实现状态与待定决策 | 账期规则登记册 |
| [03_replay_compatibility_matrix.md](03_replay_compatibility_matrix.md) | 重复/迟到/撤回等 12 个场景的重放兼容矩阵 | 重放兼容矩阵 |
| [04_contract_retroactive_change.md](04_contract_retroactive_change.md) | 合同追溯变更的影响范围与产出物约定 | 追溯变更影响范围 |
| [05_release_rollback_runbook.md](05_release_rollback_runbook.md) | 发布前检查、发布步骤、三级回滚手册 | 发布回滚手册 |
| [06_minimal_examples.md](06_minimal_examples.md) | 跨月/阶梯边界/退款/无事件等 7 个最小样例的期望与实测 | 最小样例 |
| [07_gap_analysis.md](07_gap_analysis.md) | 当前代码与 fixture 能证明什么、哪些要求仍需开发（G1–G14） | 能力与缺口清单 |

## 阅读顺序建议

1. 先读 `01` 与 `02`，建立字段与规则词汇表；
2. 再读 `03` 矩阵与 `06` 样例，把规则映射到可运行输入；
3. `04` 是追溯变更专题，`05` 是上线/回退操作手册；
4. 最后读 `07`，所有文档中的 `G*` 编号均在该文件汇总。

## 关键事实（先记住）

- 当前引擎 `src/usage_contract_replay/engine.py` 只认识扁平合同
  `{free, unit_price, refund}`，对全部事件**不分账户、不分账期、不分类型**地求和。
- 事件里的 `at` / `ingested_at` / `event_id` / `status` 字段当前**完全没有被读取**。
- 当前输出是确定性的纯函数（同输入两次运行字节一致，已验证），但只覆盖最简单的
  "总量 − 免费额度 × 单价 − 退款标量"一条路径。
- `fixtures/input.json` 是唯一的 v1 金色输入；`fixtures/examples/` 下是 v2-draft 目标
  样例，**在当前代码上全部以 `KeyError: 'unit_price'` 失败**，这是预期内的缺口证据。
