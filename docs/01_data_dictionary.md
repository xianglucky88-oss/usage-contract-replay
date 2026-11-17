# 01 · 合同 / 用量数据字典

> 约定：**原始事件**是外部计量系统写入、追加不可变（append-only）的输入事实；
> **派生金额**是重放引擎从原始事件 + 合同版本计算出的结果，任何时候都可以被重新计算，
> 因此派生金额不属于事实，禁止手工改写。
>
> 字段分两代：
> - **v1（当前代码 `0.1.0` 实际支持）**：扁平结构，见 `fixtures/input.json`。
> - **v2-draft（目标约定，样例见 `fixtures/examples/`）**：带事件身份、合同版本、
>   账期窗口与阶梯价格。当前代码无法解析，运行即 `KeyError: 'unit_price'`。

---

## 1. 原始事件（raw event）

事件是计量管道的事实记录。写入后不可就地修改；纠正只能通过新的追加事件
（重复投递、撤回 tombstone、迟到事件）表达，这是 R1–R4 的数据基础。

### 1.1 v2-draft 字段（目标）

| 字段 | 层级 | 类型 | 必填 | 含义 / 约束 | v1 是否存在 |
| --- | --- | --- | --- | --- | --- |
| `event_id` | 原始 | string | 是 | 事件全局唯一身份标识。去重（R1）与撤回引用（R3）的主键，必须由上游保证幂等 | 否 |
| `account` | 原始 | string | 是 | 账户标识。重放按账户分组，账户是发票的归属键 | 是（但未用于分组） |
| `event_type` | 原始 | enum | 是 | `usage` 用量 / `refund` 退款 / `retraction` 撤回。非 `usage` 事件不得计入用量合计 | 否 |
| `at` | 原始 | date(RFC3339 日期或时间戳) | 是 | 事件**业务发生时间**。账期归属（R5）与合同版本选择（R7）均以它为准 | 是（存在但引擎从不读取） |
| `ingested_at` | 原始 | RFC3339 时间戳 | 是 | 平台**摄入时间**。仅用于迟到判定（R4）与审计排序，不参与账期归属 | 否 |
| `units` | 原始 | integer ≥ 0 | 是 | 计量数量。`refund`/`retraction` 可为 0；负数量非法（见 G3） | 是（`int()` 强转，无校验） |
| `status` | 原始 | enum | 是 | `active` / `retracted`。被撤回事件在重放时按 R3 排除 | 否 |
| `related_event_id` | 原始 | string | 条件必填 | `refund` 关联的原用量事件、`retraction` 指向的被撤回事件 | 否 |
| `amount` | 原始（仅 refund） | decimal | 条件必填 | 退款事件的**货币金额**（非数量）。退款只冲减金额，不回冲用量（见 R10） | 否 |
| `reason` | 原始 | string | 否 | 退款/撤回原因码，审计用，如 `service-credit`、`meter-error` | 否 |
| `idempotency_key` | 原始 | string | 否 | 上游幂等键；与 `event_id` 同时存在时以 `event_id` 为去重主键 | 否 |

**事件层不变量（目标）**

1. `(event_id)` 唯一；同一 `event_id` 的重复投递（含不同 `ingested_at`）按 R1 折叠。
2. 事件只追加：撤回通过新增 `event_type=retraction` 并把原事件置 `status=retracted` 表达，
   不允许删除或覆盖原行。
3. `at` 与 `ingested_at` 是两个时钟，禁止用摄入时间替代业务时间做账期归属。
4. `units` 非负；退款用独立的 `amount` 表达货币影响。

### 1.2 v1 实际字段（当前）

| 字段 | 类型 | 当前处理 | 风险 / 缺口 |
| --- | --- | --- | --- |
| `account` | string | 原样透传；输出取 `rows[0]["account"]` | 多账户事件被合并成一张发票，只报第一个账户（实测：A1+A2 → account=A1，见 G2） |
| `at` | string | **完全不读取** | 无账期、无跨月概念（G4、G5） |
| `units` | 可被 `int()` 转换的任意值 | 全部求和 | 负数、缺失、非数字均无校验；负数经 `max(0,...)` 后被静默吞掉（G3） |

`parser.load_records` 只校验 `events` 是 list，并**静默丢弃其中非 dict 的元素**
（实测 `["junk", 42, None]` 被无声跳过，G12），不产生任何错误或告警。

---

## 2. 合同（contract）

### 2.1 v2-draft 字段（目标）

| 字段 | 层级 | 类型 | 含义 / 约束 | v1 对应 |
| --- | --- | --- | --- | --- |
| `contract_id` | 配置 | string | 合同标识，一个账户一份版本化合同 | 无 |
| `versions[]` | 配置 | array | 合同版本序列，按 `effective_from` 排序，区间互不重叠且连续 | 无（单份扁平合同） |
| `versions[].version` | 配置 | integer | 单调递增版本号 | 无 |
| `versions[].effective_from` | 配置 | date | 生效日（含当日）。版本选择规则 R7：取满足 `effective_from <= 事件 at` 的最新版本 | 无（无版本概念，G6） |
| `versions[].free_quota` | 配置 | integer | 账期级免费额度。按账户 × 账期 × 适用合同版本各给一次（R8） | `free` |
| `versions[].currency` | 配置 | string | 三字母币种码；同一发票行不得混币种 | 无（G13） |
| `versions[].tiers[]` | 配置 | array | 阶梯价格表，按 `up_to` 升序；末档 `up_to = null` 表示开放 | `unit_price` 单档 |
| `tiers[].up_to` | 配置 | integer/null | 本档上限（边际口径，见 R9）；边界样本 `units == up_to` 全落在本档 | — |
| `tiers[].unit_price` | 配置 | decimal | 该档边际单价 | `unit_price` |

### 2.2 v1 实际字段（当前 `engine.calculate`）

| 字段 | 类型 | 默认 | 当前语义 |
| --- | --- | --- | --- |
| `free` | int | `0` | 对**全部事件总量**一次性扣减，无账户/账期/版本维度 |
| `unit_price` | decimal | **无默认，缺失即 `KeyError`** | 单一单价，全量乘算 |
| `refund` | decimal | `0` | 合同级标量，直接从金额中扣减；可使账单变负（实测退款超额 → `amount=-5.0`，G9） |

> v1 的 `refund` 是配置标量，不是事件；目标 v2-draft 把退款改为**退款事件**
> （`event_type=refund` + `amount`），二者不可混用，迁移期兼容策略见
> [03 矩阵](03_replay_compatibility_matrix.md) 第 4 节。

---

## 3. 派生金额与发票字段（derived）

以下字段全部由重放计算产生，**禁止持久化手工值**；它们的定义即回归测试断言。

| 字段 | 层级 | 定义（目标 v2-draft） | 当前 v1 是否产出 |
| --- | --- | --- | --- |
| `total_units` | 派生 | 账期内、该账户、`status=active` 的 `usage` 事件去重后的 `units` 合计（R1+R3） | 是，但口径是"所有行未去重求和" |
| `billable_units` | 派生 | `max(0, total_units − 适用免费额度)`，免费额度按 R8 适用 | 是，但只对合并总量扣一次 |
| `tier_breakdown[]` | 派生 | 按 R9 边际阶梯拆分的 `{from, to, units, unit_price, subtotal}` 明细 | 否（G7） |
| `gross_amount` | 派生 | 阶梯各档小计之和 | 未单列（隐含在 amount 中） |
| `refund_amount` | 派生 | 账期内有效退款事件 `amount` 合计 | 否；v1 只有合同标量 `refund`（G9） |
| `amount` | 派生 | `gross_amount − refund_amount`，按 R11 舍入；不得为负，负差转信用余额（待定，见 07 O5） | 是，`round(..., 2)` 且**允许为负** |
| `event_count` | 派生 | 参与本次重放的有效（去重、未撤回）事件数 | 是，但统计的是 parser 输出的原始行数 |
| `invoice_id` | 派生 | `账户 + 账期` 决定的稳定身份，重放同一键必须复算同一发票 | 否（G10） |
| `invoice_status` | 派生 | `draft` / `frozen`，冻结语义见 R12 | 否（G11） |
| `applied_contract_version` | 派生 | 本发票实际使用的合同版本号（审计追溯用） | 否（G6） |

当前外层信封（`report.build_result`，保持稳定、勿轻易改键）：

```json
{ "project": "usage-contract-replay", "status": "ok", "...": "引擎 data 平铺合并" }
```

注意：`status` 是**管道状态**而非发票状态；目标中的 `invoice_status` 是新增的业务字段，
两者命名需要在开发时区分（见 07 O8）。

---

## 4. 顶层包裹字段

| 字段 | v2-draft | v1 | 说明 |
| --- | --- | --- | --- |
| `schema_version` | 必填，如 `2.0-draft` | 无 | 版本路由依据；缺失时按 v1 处理（兼容矩阵第 4 节） |
| `billing_period.start/end` | 必填，月历区间 | 无 | 重放窗口；当前代码无窗口概念（G4） |
| `accounts` | 可选 | 无 | 显式声明"本期待出账账户"，无事件账户零账单靠它发现（R12 前置，G8） |
| `events` | 数组 | 数组 | 见第 1 节 |
| `contract` | 版本化对象 | 扁平对象 | 见第 2 节 |

---

## 5. 字段血缘（lineage）速查

```
原始事实: event_id, account, event_type, at, ingested_at, units, status,
          related_event_id, amount(refund), reason
配置事实: contract_id, versions[].(effective_from, free_quota, currency, tiers)
   │
   │  parser 校验/抽取 → 去重(R1) → 撤回过滤(R3) → 账期归属(R4/R5)
   │  → 合同版本选择(R7) → 免费额度(R8) → 边际阶梯(R9) → 退款冲减(R10) → 舍入(R11)
   ▼
派生结果: total_units, billable_units, tier_breakdown, gross_amount,
          refund_amount, amount, event_count, invoice_id, invoice_status,
          applied_contract_version
```

当前代码实际只有一条血缘：`units → sum → max(0, −free) → ×unit_price → −refund → round2`，
其余节点全部缺失，缺口编号见 [07_gap_analysis.md](07_gap_analysis.md)。
