# 试剂库存服务与独立检验运行库 (719)

Python 3.11 + SQLite 的试剂库存服务与运行库。只使用 `materials/719` 中的
合成试剂和题定规则；不生成实验建议。

## 运行

```bash
python3 -m unittest discover -s tests -v     # 72 个测试
python3 demo_719.py                          # 跑 719 操作文件并打印可手算守恒
python3 demo_billing.py                      # 点数券结费 + 汇款清算三种结果
```

CLI 独立入口（`--db` 指向 SQLite 文件）：

```bash
python3 -m reagent --db store.db init --inventory materials/719/inventory.json

# 库存侧
python3 -m reagent --db store.db query       --bottle U --day 14
python3 -m reagent --db store.db snapshot    --day 14
python3 -m reagent --db store.db conservation
python3 -m reagent --db store.db open        --bottle U --day 11
python3 -m reagent --db store.db hold        --holder-id h1 --day 10 --bottle U=1
python3 -m reagent --db store.db consume-hold --holder-id h1 --day 12
python3 -m reagent --db store.db release-hold --holder-id h1 --day 12 --reason abort
python3 -m reagent --db store.db get-hold    --holder-id h1 --day 12

# 运行侧
python3 -m reagent --db store.db revise  --method-id M --revision 2 --requirement R=2 --day 12
python3 -m reagent --db store.db create  --run-id r1 --method-id M --method-revision 1 \
                                          --day 10 --bottle U=1
python3 -m reagent --db store.db start   --run-id r1 --day 12
python3 -m reagent --db store.db release --run-id r1 --day 12 --reason cancel
python3 -m reagent --db store.db get-run --run-id r1
python3 -m reagent --db store.db run-ops --inventory materials/719/inventory.json \
                                          --operations materials/719/operations.json
```

## 关键规则

**有效期**（`reagent/bottles.py`）

- 未开封：`intake_day ≤ day < printed_expiry_day`
- 已开封：`intake_day ≤ day < min(printed_expiry_day, opened_day + L)`
- 边界当天不可用；`L` 与印刷期只取更早者，不会互相延长。
- 开封日只能由“实际使用”或显式人工开封设置；库触发器 `bottles_open_immutable`
  禁止清空或改写已开封日。

**守恒与“可领”分离**（`bottle_allocation` 视图 + `reagent/bottles.py`）

- `unallocated = total − reserved − consumed`，永远由预留行推导，不另存计数。
- `reserved` 是状态为 `active` 的预留之和；`consumed` 是 `consumed` 之和；
  `released` 两者都不计，份数回到未占。
- `leadable_today = unallocated if 当日有效 else 0`。过期只是让份数不可领，
  不会把它从未占里抹掉。SQL 视图和 Python 推导在 `conservation` 中交叉核对。

**预留不是使用**

- `create` / `hold` 只写 `active` 预留，不设开封日、不消耗开封寿命。
- 只有成功的 `start` / `consume-hold` 才在同一事务内把 `active → consumed`
  并给未开封瓶写入本次实际使用日；已有开封日不重置。
- 失败请求整体回滚：不会给任何瓶留下开封事实。

**运行状态机**（`reagent/runs.py`）

- `create` 在一个 `BEGIN IMMEDIATE` 事务里校验方法需求 + 选择 + 当日效期，
  然后一次性写入全部预留；任何一步失败都不留半个运行或半组预留。
- `start` 重新核对当日效期，把该运行的全部 `active` 预留原子转成 `consumed`。
  过期导致失败时释放该运行的全部预留、记录原因，并使运行进入 `released` 终态。
- 终态运行不可再消耗、释放或返还；重复 `start` 返回**原样保存**的终态回执
  （`runs.terminal_json`），不会按之后的库存重算。
- 选择按种类合计必须恰等于方法需求，可同种分摊多瓶，不能多领、少领或用异种替代。

**库存事实只有一份**

- 预留行用通用 `holder_id` 标识；运行直接以 `run_id` 作 holder，所以运行视图
  中的数量是对同一批预留行的 `GROUP BY`，不存在第二份互不约束的计数。
- `tests/.../test_run_quantities_are_derived_from_inventory_rows` 直接改预留行，
  运行视图随之变化，证明没有副本。

**并发与身份**

- `BEGIN IMMEDIATE` 先拿写锁，配合 `busy_timeout` 串行化“最后一份”的争用；
  两连接中至多一方预留成功，另一方读到 0 未占后以 `InsufficientStock` 失败。
- 外部 holder 不能占用某运行的 `run_id`，运行也不能复用已存在的 holder，
  避免绕过运行状态机结算预留。

**方法修订**

- 新修订写入 `methods`/`method_requirements`，只影响之后新建的运行。
- 运行在创建时把需求快照写入 `run_requirements`（触发器保证不可改）；
  新需求仍受瓶的实际有效期约束。

## 719 结果（`demo_719.py` 输出）

| 操作 | 结果 |
| --- | --- |
| U 10 日预留 1 | 未开封，`reserved=1`，`unallocated=3` |
| U 12 日 start | 开封日=12，`consumed=1` |
| new-need (v2) 12 日 start | 再消耗 2，`consumed=3`，`unallocated=1` |
| U day 13 | `unallocated=1`，`leadable_today=1` |
| U day 14 | `unallocated=1`，`leadable_today=0`（开封 12 + L2 到期） |
| P 10 日预留 1，11 日 start | 失败并释放；`unallocated=2`、`reserved=0`、`consumed=0`、`leadable=0` |
| LAST 两连接争用 | 恰一方成功，另一方 `InsufficientStock`，胜者不设开封 |
| 重复 start reserve-U | 返回原终态，不再扣 1 |

守恒终态：`U 4=1+0+3`、`P 2=2+0+0`、`LAST 1=0+1+0`。

## 点数券结费（`reagent/billing.py` + `reagent/billing_match.py`）

**真领用凭据**

- 只有 `consumed` 预留行产生凭据，按份拆成 `(reservation_id, seq)`，运行与
  普通 holder 都按已登记的 `holder_id` 入账；`released`、仍 `active` 的仅预留
  行和失败重放不产生任何凭据。
- 券效按**真实结算日** `reservations.settled_day` 判定，与预留日无关；可用区间
  是半开的 `[valid_from_day, valid_until_day)`，止日当天不可用，券可跨月。
- 没用上的券不退款；关账不会提前注销尚未到期的券，过期券也保留在账上。

**核销优化（一券一份、一份一券）**

- 抵额 = `min(标价, 券上限)`；自己的券抵自己不产生还款，用掉别柜的券时偿还
  购买柜该券的**已付价**，未抵差额付给供应商。
- 目标字典序：① 供应商本期收到的现金总额最少（即抵额总和最大）；② 平手时把
  凭据按 `(预留号, 份内序号)` 排序，取券号序列字典序最小者，未用排在所有券号
  之后。两个目标编码进同一个费用格，由矩形匈牙利算法一次求出（`billing_match.py`）。

**期间、重开与关账**

- 每期至多一张活动账单。`reopen` 把活动账单置为 `superseded`（回执原样保留，
  类似运行的 `terminal_json`），释放其凭据与券后可重新出账；`closed` 账单的
  核销被触发器冻结，券永久锁定、期间不可重开。旧期间的凭单因此可重开。

## 清算席（`reagent/clearing.py`）

- 按结费给出的各柜净额（应收为正、应付为负，总和为零）找全额清账方案。
- 每条有向线路有整批容量与一次性启用费，允许中转；启用费由费用账另付，不进
  节点余额。
- 至多 14 条线路，故穷举线路子集（按总启用费升序），每个子集用一次最大流做
  下界/精确边可行性检验；再逐条把金额 pin 到最小可行整数，得到确定、可重放的
  逐条指令。无可行子集时不生成任何半批指令。
- 确定性：同一输入多次调用返回完全相同的逐条金额。

```bash
# 结费侧
python3 -m reagent --db s.db period-open --period-id p1 --day 30
python3 -m reagent --db s.db price  --period-id p1 --bottle X --price 5
python3 -m reagent --db s.db voucher --document voucher.json
python3 -m reagent --db s.db credentials --period-id p1
python3 -m reagent --db s.db bill --period-id p1 --day 30      # 再调一次即重放
python3 -m reagent --db s.db settlement --period-id p1
python3 -m reagent --db s.db period-reopen --period-id p1      # 活动账单可重开
python3 -m reagent --db s.db bill-close --period-id p1         # 关账后冻结
# 清算侧（document 含 balances 与 routes）
python3 -m reagent --db s.db clear --document routes.json
```

## 范围

至 100 瓶 / 200 运行已在 `ScaleTests` 覆盖。结费侧每期至多 200 份凭据、
200 张券（`MAX_CLAIMS_PER_BILL` / `MAX_VOUCHERS_PER_BILL`），核销引擎的规则
正确性另由 200 例随机暴力对照（穷举所有券-凭据配对）保证；清算侧限定 8 个
节点、14 条线路，最小启用费与逐线金额由 150 例随机暴力对照（穷举所有整数
流量组合）校验。不做分布式事务、进程强杀、真实仪器通信或结果判读。终态收束
依赖单库事务 + 保存回执：开始失败时在同一事务内释放全部预留并写终态原因，
之后的重放只读该回执。
