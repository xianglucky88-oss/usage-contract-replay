# 07 · 能力与缺口分析（当前代码 / fixture 能证明什么）

本文档是整套交接的事实基线：所有"已证明"均有可复跑命令或测试，所有"待开发"均给出
缺口编号并被前述文档引用。本次维护**不实现任何缺口**。

## 1. 当前代码与 fixture 能证明的事

| # | 已证明能力 | 证据 |
| --- | --- | --- |
| P1 | 分层边界清晰：parser 抽取 → engine 纯计算 → report 信封 → pipeline 编排 → CLI 输出，各模块可单独导入 | 源码结构 + `tests/test_core.py` 分别调用三层 |
| P2 | 单账户、单账期、单档单价的扁平计费主路径正确：`sum(units) → 减 free → ×unit_price → 减 refund → 两位小数` | fixture 输出 `amount=25.0`（20 单位、free=10、单价 2.5），测试断言通过 |
| P3 | 空事件不崩溃：空列表走 `rows[0] if rows else None` 分支，输出 `account=None, amount=0.0` | 探针实测（附录 A.2） |
| P4 | 输出确定性 / 可重放：无随机数、无时钟读取、无 I/O 副作用，`sort_keys=True` | 同一 fixture 连跑两次 `diff` 字节一致 |
| P5 | 顶层输出信封稳定：`project=usage-contract-replay`、`status=ok`，data 平铺 | 测试断言 + CLI 实测 |
| P6 | 零第三方依赖、标准库即可运行，Python 3.9+ 声明与 3.11 实测一致 | `pyproject.toml` + 运行记录 |
| P7 | 结果对象不可变：`PipelineResult` 为 `frozen=True` dataclass | `models.py` |
| P8 | 输入为本地确定性 JSON，无网络/凭据依赖，离线可复现 | README 与实际运行 |

## 2. 测试覆盖的真实范围

`tests/test_core.py` 只有 1 个用例，实际断言：

1. parser 对 fixture 返回非空列表；
2. engine 返回非空 dict；
3. pipeline 输出 `status=ok`、`project` 正确；
4. `amount == 25.0`。

未覆盖：错误输入、多账户、空事件、负数/缺字段、时间归属、去重、撤回、退款事件、
阶梯、合同版本、发票状态、输出 schema 的其他键（`total_units` 等从未被断言）。
即 P1–P8 中只有 P2 的一个数值点和 P5 的两个键有自动化断言，其余靠本次人工探针。

## 3. 缺口登记册 G1–G14

| 编号 | 缺口 | 现状证据 | 影响的规则/文档 | 优先级 |
| --- | --- | --- | --- | --- |
| G1 | 无事件身份：缺 `event_id`，无去重（R1）、无撤回引用（R3）；重复事件双计、撤回事件被当用量 | 探针：两条 15 单位 → 50.0；样例 05/07 | R1、R2、R3；矩阵 2/3/4 | P0 |
| G2 | 无账户分组：多账户事件合并，输出只取 `rows[0]` 账户 | 探针：A1+A2 → `account=A1, amount=37.5`；矩阵 11 | R12；数据字典 §1.2 | P0 |
| G3 | 无输入校验：负 `units` 被 `max(0,...)` 吞掉，非法类型无报错 | 探针：`units=-3` → `amount=0.0` 无告警 | R1–R3 前置；矩阵 10 | P0 |
| G4 | 无时间维度：`at`/`ingested_at` 从不读取，无账期窗口、无跨月切分、无迟到归属 | 探针：1 月+2 月事件合并为 25.0；样例 01/06 | R4、R5、R6 | P0 |
| G5 | 无账期声明输入：v1 顶层没有 `billing_period`，重放窗口无法表达 | 现 fixture 结构 | R5 | P1 |
| G6 | 无合同版本：无 `versions/effective_from`，版本化输入直接 `KeyError` | 7 个 v2 样例全部 exit 1 | R7；追溯文档全文 | P0 |
| G7 | 无阶梯价格：只支持单一 `unit_price` 全量乘算 | 样例 02 `KeyError` | R9 | P0 |
| G8 | 免费额度无账户/账期/版本维度，合并总量只扣一次；跨期结转策略未定 | 样例 01 实测合并扣减 | R8、R6 | P0 |
| G9 | 退款仅为合同标量、可产生负账单；无退款事件、无关联、不保护用量口径 | 探针：零用量 refund=5 → `amount=-5.0`；样例 03 | R10 | P0 |
| G10 | 无发票身份 `invoice_id`，输出是匿名 dict，无法做稳定 upsert 与追溯引用 | 空事件输出 `account=None`；样例 04 | R12；追溯文档 | P0 |
| G11 | 无 `draft/frozen` 状态机，重放无法区分可覆盖与禁改写 | 源码无状态字段 | R12；矩阵 frozen 列 | P0 |
| G12 | parser 静默吞数据：非 dict 元素被无声丢弃；缺键抛裸 `KeyError` 而非结构化错误 | 探针：`["junk",42,None]` 仅剩合法行；v2 样例栈追踪 | 矩阵 10；发布 C13 | P1 |
| G13 | 浮点金额 + 银行家舍入、无币种字段：`round(2.675,2)=2.67`（二进制浮点 + round-half-even） | 探针实测 | R11 | P1 |
| G14 | 无持久化/编排/审计层：事件存储、发票落库、补差生成、影响报告、归档均不存在，L2/L3 回滚不可执行 | 仓库仅内存管线 | R2；追溯文档；回滚 L2/L3 | P1 |

依赖顺序建议（仅登记，不在本次实施）：G3/G12 先立校验边界 → G1 身份 → G2 分组 →
G4/G5 账期 → G6 版本 → G8 额度 → G7 阶梯 → G9 退款 → G13 金额 → G10/G11 发票 → G14 编排。

## 4. 当前代码中需要特别小心的既有行为（开发时勿当成"正确基线"复制）

1. `engine.calculate` 是单行表达式，`int(r["units"])` 对字符串数字隐式转换、对非法值
   抛 `ValueError`，行为未文档化；
2. `max(0, total - free)` 同时掩盖了负用量（G3）和免费额度兜底两件事，重构时要拆开；
3. `round(float, 2)` 是 round-half-even 且基于二进制浮点，与财务常见的 half-up 不符（G13）；
4. `report.build_result` 用 `**data` 平铺，若未来 data 含 `project/status` 键会静默覆盖
   信封键，schema 设计时需保留命名空间；
5. parser 返回的是 `dict(row)` 浅拷贝，嵌套字段仍与输入共享引用；
6. CLI 对文件缺失、JSON 语法错误、schema 不符均无友好处理，直接抛栈（G12）。

## 5. 待裁决事项（open decisions）

| 编号 | 议题 | 候选 | 影响 |
| --- | --- | --- | --- |
| O1 | 免费额度是否跨月结转 | 每期重置（样例默认）/ 按合同约定结转 | R8、样例 01 期望值 |
| O2 | 阶梯是否支持封顶价/打包价档位 | 仅边际档（当前约定）/ 增加套餐档 | R9、tier schema |
| O3 | 币种缺省值与多币种处理 | 默认 USD / 强制显式 / 按账户币种表 | R11、G13 |
| O4 | 同 `event_id` 内容冲突的策略 | 整批拒绝入死信（文档默认）/ 以最新摄入为准 | R1、矩阵 3 |
| O5 | 退款超额（负账单）落点 | 转信用余额（文档默认）/ 允许负发票 / 挂应收红字 | R10、G9 |
| O6 | v1 标量 refund 与 v2 退款事件迁移期共存多久 | 单版本共存 + 双来源标注（文档默认）/ 一次性迁移 | 兼容矩阵 §4 |
| O7 | 追溯变更跨币种如何重算 | 禁止跨币种改写（文档默认）/ 引入汇率快照表 | 追溯文档 §3 |
| O8 | `status`（管道）与 `invoice_status`（业务）命名空间 | 业务键统一加前缀 / 嵌套 `invoice` 对象 | 数据字典 §3 |
| O9 | 关账冻结窗口的具体时长与发布封网时刻 | 期末 ±1 工作日（手册默认）/ 财务日历驱动 | 发布手册 §3.2 |

## 6. 附录 A：本次交接的探针记录（可复跑）

### A.1 环境与主路径

```bash
$ PYTHONPATH=src python3 -m unittest discover -s tests -v
# Ran 1 test ... OK
$ PYTHONPATH=src python3 -m usage_contract_replay.cli
# {"account":"A1","amount":25.0,"billable_units":10,"event_count":2,
#  "project":"usage-contract-replay","status":"ok","total_units":20}
```

### A.2 边界探针（内存调用，输出为实测原文）

```text
round(2.675,2) = 2.67                              # G13 浮点/舍入
refund overshoot: {'account': None, ..., 'amount': -5.0}   # G9 负账单
negative units: {'account': 'A1', 'total_units': -3,
                 'billable_units': 0, 'amount': 0.0, ...}  # G3 负数被吞
mixed accounts: {'account': 'A1', 'total_units': 15,
                 'billable_units': 15, 'amount': 37.5, ...} # G2 多账户合并
parser drop: [{'account': 'A1', 'units': 1}]      # G12 静默丢行
v1 duplicate: amount=50.0（目标 12.5）             # G1 重复双计
v1 retraction: amount=5.0, event_count=2（目标 0.0/0）      # G1 撤回无效
```

### A.3 v2-draft 样例批量运行

```bash
for f in fixtures/examples/*.json; do
  PYTHONPATH=src python3 -m usage_contract_replay.cli "$f"
done
# 7/7 均以 KeyError: 'unit_price' 退出（exit 1），栈顶位于 engine.py:2
```

### A.4 确定性验证

```bash
PYTHONPATH=src python3 -m usage_contract_replay.cli > /tmp/run1.json
PYTHONPATH=src python3 -m usage_contract_replay.cli > /tmp/run2.json
diff /tmp/run1.json /tmp/run2.json   # 无输出，字节一致
```

## 7. 结论

当前仓库是一个**确定性良好、边界清晰但口径极粗的单路径原型**：扁平 v1 主路径可信
（P1–P8），而题目要求的事件身份、合同生效、免费额度维度、阶梯价格、退款事件、
跨月归属、账单冻结七项约定，在代码中均不存在（G1–G14），已由 7 个最小样例和
探针全部复现为可审计证据。后续开发应以 `docs/02` 的 R1–R12 为规格、以
`fixtures/examples/` 为失败先行的 golden 测试、以 `docs/05` 的 C9–C14 为上线门槛，
并在动工前先裁决 O1–O9。
