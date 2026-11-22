# 试剂库存服务与独立检验运行库 (719)

Python 3.11 + SQLite 的试剂库存服务与运行库。只使用 `materials/719` 中的
合成试剂和题定规则；不生成实验建议。

## 运行

```bash
python3 -m unittest discover -s tests -v     # 77 个测试
python3 demo_719.py                          # 跑 719 操作文件并打印可手算守恒
python3 demo_deposit.py                      # 容器退押 + 清退 + 装车优化并落盘结果
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

# 容器退押 / 清退 / 装车（虚构账务数据）
python3 -m reagent --db store.db credential      --bottle A
python3 -m reagent --db store.db explain-voucher --bottle B --day 12
python3 -m reagent --db store.db recount         --bottle B --day 12
python3 -m reagent --db store.db open-clearance  --bottle B --portions 1 --day 12
python3 -m reagent --db store.db sign-clearance  --request-id CLR:B \
                                                 --counter-id count-1 --day 12
python3 -m reagent --db store.db apply-voucher   --bottle A --day 12
python3 -m reagent --db store.db add-trip        --trip-id T12 --departure-day 12 \
                                                 --arrival-day 12 --max-boxes 3
python3 -m reagent --db store.db add-box         --model-id TWO --slots 2 --kind R --kind S
python3 -m reagent --db store.db add-offer       --trip-id T12 --model-id TWO --price 5
python3 -m reagent --db store.db add-supplier-kind --supplier SUP --kind R
python3 -m reagent --db store.db plan            --day 12
python3 -m reagent --db store.db ship            --day 12 --shipment-id SHP12
python3 -m reagent --db store.db vouchers
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

## 容器退押与装车（`reagent/deposits.py`）

包装号、押金、规格与运费均为虚构账务数据。每只瓶有一份随实物固定的
supply credential（`bottle_credentials`：供应商、包装型号、整数押金、最迟交还日），
写入后不可改。

**可退凭单（voucher）与份数无关**

- 一份凭单对应一只实物瓶，不是份数；`unallocated=0` 只可能来自真实消耗或
  两人签字的实物清退。过期让份数不可领，但份数仍留在瓶里，不能据此领凭单。
- 可申请条件：无 `active` 预留 + 全部份数已实际消耗或已清退 + 当日不晚于
  最迟交还日。`explain-voucher` 独立列出每个阻断原因，库房可逐瓶查询。
- 同一瓶最多一张尚存凭单（部分唯一索引）；已交运的凭单被触发器锁定为不可改。
- 份数因过期不可领 ≠ 交出了空瓶：过期未占份必须先由两名不同盘点员对
  **同一盘点版本**和**准确的剩余未占量**签字清退（`clearance_signoffs`
  签名版本/份数须完全一致），才可申请；有人仍预留时禁止清退。

**清退是第四种去向**（`bottle_allocation`）

- `total = unallocated + reserved + consumed + cleared`；`cleared` 单列，
  绝不计入 `consumed`。数据库触发器同时约束 `reserved+consumed+cleared ≤ total`。

**装车最优**

- 回收站有若干趟车，各有出发日、到达日、最大箱数；每趟可买若干箱型，各有
  价格、格数、允许的瓶型。一箱只寄一家供应商、只属于一趟车；供应商可只收
  某几种瓶型（`supplier_kinds`），同箱可在箱型允许时混收。
- 过交还日不付押金：只有**到达日 ≤ 最迟交还日**的凭单才计押金。
- 目标是 `实退押金 − 箱费` 最大，允许一瓶也不寄；同利润优先多退瓶。
- 精确搜索 + 后缀上界剪枝 + 同状态记忆化（`_optimize`），并与暴力枚举
  交叉验证。交运后凭单锁定，另一张单不能再装同瓶（`shipped_items` 唯一约束）。

### 719 退押场景（`demo_deposit.py`，结果落盘 `materials/719/deposit_results.json`）

A、B 同型，各押 4、交还日 12。A 10 日经普通运行耗尽；B 剩 1 份因过期不可领，
只有一人签过清退。车 12 日到，两格箱费 5：

| 步骤 | 结果 |
| --- | --- |
| 仅 A 可退时 `plan` 12 | 一瓶 4 − 箱 5 < 0，最优**不寄** |
| 第二盘点员在同版核准 B 后 `plan` 12 | 两瓶 8 − 5，净得 **3**，装一箱 T12/TWO |
| 同样两张凭单，仅车改 13 日到 | 到达晚于交还日 12，**仍不寄** |
| 两人签不同版本 | 请求停在 `pending`，凭单被阻断 |
| 过期前已被旧 holder 预留、过期时仍占用 | 有 `active` 预留，清退与凭单均**阻断** |
| 守恒终态 | A `1=0+0+1+0`、B `1=0+0+0+1`，C/D 各自 `1=1+0+0+0` |

## 范围

至 100 瓶 / 200 运行已在 `ScaleTests` 覆盖。不做分布式事务、进程强杀、
真实仪器通信或结果判读。终态收束依赖单库事务 + 保存回执：开始失败时在同一
事务内释放全部预留并写终态原因，之后的重放只读该回执。
