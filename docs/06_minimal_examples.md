# 06 · 最小样例集（期望行为 vs 当前实测）

样例目录：`fixtures/examples/`，全部采用 [01 数据字典](01_data_dictionary.md) 定义的
`schema_version=2.0-draft` 目标结构。题目要求的四类（跨月、阶梯边界、退款、无事件账户）
为 01–04；为支撑[重放兼容矩阵](03_replay_compatibility_matrix.md)另附 05–07
（重复、迟到、撤回）。

## 运行方式

```bash
# 目标样例（当前代码预期失败）
PYTHONPATH=src python3 -m usage_contract_replay.cli fixtures/examples/01_cross_month.json
# 现有 v1 金色输入（当前代码通过）
PYTHONPATH=src python3 -m usage_contract_replay.cli fixtures/input.json
```

## 实测总览（本次交接逐个运行）

| 样例 | 场景 | 当前代码结果 | 目标结果（期望） |
| --- | --- | --- | --- |
| `01_cross_month.json` | 跨月归属 | ❌ `KeyError: 'unit_price'`（exit 1） | 两张发票：1 月 5.00 / 2 月 0.00 |
| `02_tier_boundary.json` | 阶梯边界 | ❌ `KeyError: 'unit_price'` | 单张发票 200.00（100 单位全在 2.0 档） |
| `03_refund.json` | 退款事件 | ❌ `KeyError: 'unit_price'` | 25.00 − 10.00 = 15.00，用量仍为 10 |
| `04_no_event_account.json` | 无事件账户 | ❌ `KeyError: 'unit_price'` | A4 零金额发票 0.00 |
| `05_duplicate_event.json` | 重复事件 | ❌ `KeyError: 'unit_price'` | 去重后 12.50，`event_count=1` |
| `06_late_event.json` | 迟到事件 | ❌ `KeyError: 'unit_price'` | 2 月发票 0.00 + 1 月追溯重发 5.00 |
| `07_retraction.json` | 撤回 | ❌ `KeyError: 'unit_price'` | 撤回后 0.00，`event_count=0` |

全部 7 个失败的根因相同：现引擎 `engine.py` 直接索引扁平 `contract["unit_price"]`，
而 v2-draft 把单价移入 `contract.versions[].tiers[]`。这是代际差距的集中证据，不是 7 个独立 bug。

---

## 样例 01 · 跨月归属（R5/R6/R8）

输入要点：A1 在 1 月 31 日（12 单位）与 2 月 1 日（8 单位）各一条事件；
窗口声明为 `2026-01-01 ~ 2026-02-28`；免费额度 10，单价 2.5。

期望（两张独立发票，免费额度各扣一次）：

| 发票 | total_units | billable_units | 计算 | amount |
| --- | --- | --- | --- | --- |
| `A1\|2026-01` | 12 | 2 | (12−10)×2.5 | **5.00** |
| `A1\|2026-02` | 8 | 0 | max(0, 8−10) | **0.00** |

当前实测：v2 输入 `KeyError`；把同样数据压成 v1 扁平输入时，引擎跨月合并为
`total_units=20, billable_units=10, amount=25.0` 一张发票——免费额度只扣一次，
正是 R6 要消除的错误口径。

## 样例 02 · 阶梯边界（R9）

输入要点：A2 单条 100 单位事件；阶梯 `(0,100]@2.0`、`(100,∞)@1.0`，无免费额度。

期望（边际口径，边界值全落本档）：

- 第 1 档：100 单位 × 2.0 = **200.00**；第 2 档：0 单位。
- 合计 `gross_amount=200.00`，`tier_breakdown` 两档都要输出（第 2 档 subtotal=0）。
- 反例对照：若 101 单位，则 100×2.0 + 1×1.0 = 201.00。维护时用这对数值守住边界。

当前实测：`KeyError`（无阶梯能力，G7）。

## 样例 03 · 退款事件（R10）

输入要点：A3 一条 10 单位用量事件；一条 `event_type=refund` 事件，`amount=10.0`，
`related_event_id` 指回用量事件。

期望：

| 字段 | 值 | 说明 |
| --- | --- | --- |
| `total_units` | 10 | 退款**不回冲用量** |
| `billable_units` | 10 | 同上 |
| `gross_amount` | 25.00 | 10×2.5 |
| `refund_amount` | 10.00 | 来自退款事件 |
| `amount` | **15.00** | 25.00−10.00 |

当前实测：v2 输入 `KeyError`。v1 的等价物是合同标量 `refund: 10`，可算出同样的 15.00，
但语义不同：标量与事件无关、无关联键、且超额时产生负账单（探针实测 `refund=5`
零用量 → `amount=-5.0`，G9）。

## 样例 04 · 无事件账户（R12）

输入要点：`events=[]`，顶层 `accounts=["A4"]` 显式声明 A4 本期应出账。

期望：产出 `invoice_id=A4|<期>` 的零金额发票：
`total_units=0, billable_units=0, amount=0.00, invoice_status=draft`。
零发票是"该账户本期无欠费且未被遗漏"的审计凭证。

当前实测：v2 输入 `KeyError`；v1 空事件输出 `{"account": null, "amount": 0.0, ...}`——
账户身份丢失，无法证明是 A4 的零账单（G10）。

## 样例 05 · 重复事件（R1）

输入要点：两条 `event_id` 完全相同（`evt-0501`）的 15 单位事件，`ingested_at` 相差一天。

期望：R1 折叠为一条 → `total_units=15, billable_units=5, amount=12.50, event_count=1`，
另计重复投递计数 1（审计字段，不进发票金额）。

当前实测：v2 输入 `KeyError`；v1 形态探针（无 `event_id`）双计为
`total_units=30, amount=50.00`，与期望值差 4 倍量级，是 G1 的直接证据。

## 样例 06 · 迟到事件（R4/R5）

输入要点：重放窗口是 2026 年 2 月；`evt-0601` 业务时间 `at=2026-01-20` 但
`ingested_at=2026-02-03`（关账后才到）；`evt-0602` 是 2 月 10 日的 8 单位正常事件。

期望：

1. 迟到事件按业务时间归属 1 月：1 月发票重算为 (12−10)×2.5 = **5.00**（1 月原为
   零事件零发票时即新发，已有关联发票则重算/补差，状态分流见矩阵第 3 节）；
2. 2 月发票只含 8 单位：8−10 → 0，**0.00**；
3. 绝不允许把 1 月用量并进 2 月发票。

当前实测：v2 输入 `KeyError`；v1 形态探针把两月合并成 `amount=25.0`（G4）。

## 样例 07 · 撤回（R3）

输入要点：1 月 10 日 12 单位用量事件；1 月 18 日一条 `retraction` 事件通过
`related_event_id=evt-0701` 撤回它，原因码 `meter-error`。

期望：原事件置 `retracted` 并从重放集合排除 →
`total_units=0, billable_units=0, amount=0.00, event_count=0`；
撤回事件自身不计入用量、不计入 `event_count`，但保留在审计流中可查。

当前实测：v2 输入 `KeyError`；v1 形态探针中原用量照常计费
`billable_units=2, amount=5.0, event_count=2`（撤回行反而被算成一个事件，G1）。

---

## 样例与规则/缺口映射

| 样例 | 主要规则 | 缺口编号 |
| --- | --- | --- |
| 01 | R5、R6、R8 | G4、G8 |
| 02 | R9 | G7 |
| 03 | R10、R11 | G9、G13 |
| 04 | R12 | G10、G11 |
| 05 | R1、R2 | G1 |
| 06 | R4、R5 | G4 |
| 07 | R2、R3 | G1 |

这些样例在目标能力上线前是**失败的规格说明**；上线后应转为 golden 回归测试
（发布检查 C9），期望输出锁定为本表数值。
