# sql_param：独立 SQL 命名参数模板编译服务 + SQLite 批量执行库

纯 Python 3.11 标准库实现，不依赖第三方包，编译/绑定不需要数据库。

## 目录结构

```
sql_param/
  compiler.py    字符级状态机模板编译器（单引号/双引号/行注释/块注释/:name）
                 另提供 normal_view：提取已编译 SQL 的正常区域文本供合账校验
  bindings.py    值解码（null/int64/str/bytes/$bytes）与按出现顺序绑定
  plan_store.py  sql-param-plan/v1 计划发布、重放校验、持久化与公开格式校验
  executor.py    固定修订 → 整批预检 → 单事务依次执行 → 回执落盘
  slips.py       sql-param-slip/v1 离线柜台对账条出具与独立校验
  reconcile.py   回单室合账：交错顺序搜索、可行终局枚举、独立复核、整批隔离
  credit.py      授信席：最大价值签单、逐终局配给、供量缺口见证、账簿持久化
  errors.py      统一异常类型
cli/
  publish_plan.py     独立 CLI：发布单个模板为计划文件
  run_batch.py        独立 CLI：读取批次 JSON 执行并输出回执路径
  reconcile_round.py  独立 CLI：从快照核对一轮对账条并输出合账报告
  credit_round.py     独立 CLI：评估一轮新申请，可选正式签发最佳集合
demo_run.py        读取 materials/717 的完整端到端驱动（含与材料 plan_reader.py 互操作）
demo_offline.py    离线回单合账 + 授信签单端到端演示（题目 X/Y 货位场景）
tests/             71 个 unittest 用例
```

## 词法规则（为什么不能用正则替换）

编译器逐字符扫描，五个状态：正常 SQL、单引号字符串、双引号标识符、行注释、块注释。

- 单引号字符串 `'...'`，`''` 为转义；双引号标识符 `"..."`，`""` 为转义。
- 行注释 `-- ...` 到换行；**允许在文件结尾终止**。块注释 `/* ... */`，不支持嵌套。
- 字符串、标识符、注释中的所有字符（含引号、空白、换行、`:`、`?`、`;`）**逐字符保留**。
- 只有正常区域里完整的 `:name` 是参数，`name` 为 ASCII 字母开头的字母数字下划线；
  替换为单个 `?`，并按 **Python Unicode 字符索引（代码点，非 UTF-8 字节）** 记录
  `[start, end)`，位置含冒号。重复名字产生多条参数记录。
- 正常区域的 `;`（包括末尾）、`?` 一律拒绝；`:` 后不匹配合法名字也**明确报错**，
  不静默吞掉。未结束的引号/块注释拒绝。
- 只接受一条 SELECT / INSERT / UPDATE；空模板、多语句、其它语句种类拒绝。
- 原文不做任何 Unicode 归一化。

## sql-param-plan/v1 计划格式

公开 JSON 格式，字段：`format`、`template_id`、正整数 `revision`、原文 `source`、
可执行 `sql`、按出现顺序的 `parameters`（每项 `name`/`start`/`end`）。
计划可零参数。该格式与 `materials/717/plan_reader.py` 的读取合同完全对齐，
材料脚本可直接消费本服务写出的计划文件（见 `demo_run.py` 第 2 步）。

## 绑定值

| JSON 表示 | Python/SQLite 值 |
| --- | --- |
| `null` | `None` |
| 整数 | 有符号 64 位范围 `[-2^63, 2^63-1]` |
| 字符串 | `str`（原样保留，不归一化） |
| `{"$bytes": "十六进制"}` | `bytes`，空串 = 空 BLOB |

- `true`/`false` 明确拒绝（bool 虽是 int 子类也不接受）；浮点、数组、其它对象拒绝。
- 绑定字典必须**恰含**全部不同参数名：缺键、多键都拒绝。
- 同名参数出现 N 次，按参数顺序复制同一个值 N 次。

## 计划兼容、更新可见性与连接复用边界

**兼容性**

- 计划一经发布即不可变。同 `template_id + revision + source` 重放是幂等的，返回同一计划；
  同 `template_id + revision` 但 source 异文直接拒绝；新修订必须递增 revision，写入新文件，
  永不覆盖旧计划。
- 存储布局：`<store>/plans/<template_id>/r<revision>.json`，纯 JSON/UTF-8，可独立拷贝归档。
  写入走临时文件 + `os.replace` 原子替换并 fsync。
- 限额：至多 100 个模板；单批至多 100 项。

**更新可见性**

- 执行侧每次都从磁盘重新 `load` 计划并按公开合同校验，不缓存编译对象、不重新扫描 source。
  因此新发布的修订对后续批次立即生效；已完成批次的回执内嵌当时完整计划快照（含 source
  与 sql），重开进程仍可回读，不受后续修订影响。
- 批次 ID 不可变：重复执行同一 batch_id 直接拒绝，历史回执不会被覆盖。

**连接复用**

- 每个 `execute_batch` 打开独立 sqlite3 连接，`BEGIN` 显式开事务，提交或回滚后在
  `finally` 中关闭；批次之间不共享连接、不共享事务。预检阶段完全不碰数据库。
- 初始化 DDL（如 `schema.sql`）由调用方用自己的连接 `executescript` 完成；
  模板本身不允许含分号，执行库永远只跑单条参数化 SQL。

## 批量执行语义

1. 每项固定 `template_id + revision`，从保存计划读取 `sql` 和参数顺序（绝不重编译）。
2. 开事务前整批预检：计划存在、绑定键集合精确匹配、值类型合法、语句为 SELECT/INSERT/UPDATE。
   预检失败抛 `BatchError`，不开事务、不写库、不产生回执。
3. 预检通过后在一个真实事务内依次执行：
   - INSERT/UPDATE 保存 `rowcount`；
   - SELECT 保存列名与全部结果行，带 `ORDER BY` 时按游标顺序保留行序（回执标记
     `order_by: true`），未带 ORDER BY 不承诺跨次行序；
   - 任一项 SQLite 约束失败立即 `ROLLBACK`，前序写入全部撤销，回执 `status` 为
     `rolled_back` 并记录失败项错误，落盘保存，绝不标成功。
4. 回执中的 bytes 以 `{"$bytes": hex}` 表示；`ordered_values` 按计划参数出现顺序
   （含重复名字），与 `plan_reader.py` 输出语义一致。

## 使用示例

```python
from sql_param import PlanStore, BatchExecutor

store = PlanStore("./workdir/store")
store.publish("put", 1,
    "INSERT INTO notes(id, message) VALUES(:id, :text || ':literal') /* :x ? ; */")

executor = BatchExecutor(store, "./workdir/notes.db", "./workdir/receipts")
receipt = executor.execute_batch("b-ok", [
    {"template_id": "put", "revision": 1,
     "bindings": {"id": 1, "text": "hello"}},
])
```

CLI：

```bash
python -m cli.publish_plan ./workdir/store put 1 'INSERT INTO t VALUES(:id)'
python -m cli.run_batch  ./workdir/store ./workdir/notes.db ./workdir/receipts \
        b-ok batch.json
```

## 离线回单合账（sql-param-slip/v1 + sql-param-reconcile/v1）

仓库保存一张起始 SQLite 快照；多台柜台各自离线执行批次（各自的快照副本），只保留
本机批次先后（对账条 `sequence`），柜台之间没有统一时钟。

**柜台出具对账条**：`export_slip(receipt, counter_id=..., sequence=..., visibility=...)`
从 committed 回执出具对账条，每条固定一个不可拆批次。`visibility` 按 item 下标声明
公开面：SELECT 项 `{"columns": [列位...]}`，写操作项 `{"rowcount": True}`；未声明的
结果一律保密。导出方逐项核下标、列位与实际类型；零行、一行 NULL、两条相同记录是
三种不同事实，投影逐格复制、绝不折叠。回单室一侧用 `validate_slip` 独立复核同一合同。

**回单室合账**：`reconcile_round(snapshot, table=..., counters=[...])` 把快照复制进
内存库，以 SAVEPOINT 深搜全部交错顺序（按各柜台进度 + 整表状态去重），真实重放每个
批次；只有交易成功且所有公开投影与盖章 rowcount 逐项吻合，这批回单才算被解释。
模板限制逐项强制：单表（引用其它表即拒）、该表有且仅有一个 INTEGER PRIMARY KEY、
只允许参数化 SELECT/INSERT/UPDATE、正常区域不得出现函数调用（VALUES 与 INSERT 列名
括号除外）、带公开投影的 SELECT 必须显式 ORDER BY 且排序键包含主键。每轮至多 20 个
批次。报告（sql-param-reconcile/v1）列出全部可行终局，各自带一条见证顺序与逐项核对
证据；接收方可用 `verify_order` 从快照独立重放复核一条顺序，或重跑同一确定性过程
复核无解。没有任何交错顺序能解释全部批次时，整批隔离（quarantined），一个批次也
不入账。

## 授信签单（sql-param-ledger/v1）

授信席持有已签承诺账簿与一组可行终局（`capacities_from_report` 从合账报告提取各
货位可用量；回单被隔离时没有可信终局，拒绝建席）。每张申请规定数量、可提货位
（可跨所列货位拆领）与整单价值。签后必须保证：在每一个可行终局中，全体已签与
新接订单都存在同时兑现的分配（逐终局最大流判定）；实际拣货在终局确定后选。

- `evaluate(apps)`：价值最大的可签新单集合（同分取申请号字典序最小者）；各自单验
  成功不等于能同时签。
- `sign(apps)`：正式签发最佳集合（或显式给定的可行子集）并落账；同一申请不能签
  两次；下一位授信员重开同一账簿即算入仍生效的旧承诺。
- `allocate(terminal_id)`：指定终局的逐单逐货位配给。
- `gap_witness(app)`：申请不能加入时的供量缺口见证（某终局下一组订单的需求量
  超过其可用货位总供给的 Hall 型见证）。

每轮至多 12 张申请、6 个货位。

```python
from sql_param import (export_slip, reconcile_round, verify_order,
                       CreditDesk, capacities_from_report)

slip = export_slip(receipt, counter_id="jia", sequence=1,
                   visibility={0: {"rowcount": True}, 2: {"columns": [0]}})
report = reconcile_round("snapshot.db", table="bins",
                         counters=[{"counter_id": "jia", "slips": [slip_a]},
                                   {"counter_id": "yi", "slips": [slip_b]}])
terminals = capacities_from_report(report, bins={"X": 1, "Y": 2},
                                   quantity_column="qty")
desk = CreditDesk("ledger.json", terminals)
desk.evaluate(applications)   # 最佳可签集合
desk.sign(applications)       # 正式签发并落账
desk.allocate("t1")           # 指定终局的配给
```

CLI：

```bash
python -m cli.reconcile_round snapshot.db bins round.json
python -m cli.credit_round ledger.json report.json bins.json applications.json --sign
```

## 验证

```bash
python3 -m unittest discover -s tests -v   # 71 个用例
python3 demo_run.py                         # 材料全流程 + plan_reader 互操作
python3 demo_offline.py                     # 离线合账 + 授信签单全流程
```
