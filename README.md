# 独立源段目录 + 多语言交付仓

Python 3.11，SQLite。**两侧物理分离**：源段目录与交付仓各自持有一个 SQLite
数据库；交付侧通过只读连接（`mode=ro`）真实读取源目录**已发布的源产品**，
不把位置编号当身份，也不缓存源状态。文本全部为合成内容，不做自动翻译或语义评估。

## 快速运行

```bash
# 端到端跑完 materials/753 全部场景（含用只读客户端读新旧包）
python3 run_demo.py

# 仅验证真实包文件
python3 materials/753/package_reader.py data/packages/PACK1.json
python3 materials/753/package_reader.py data/packages/PACK2.json

# 测试（标准库 unittest，无需 pytest）
python3 -m unittest discover -s tests -v
```

## 目录结构

```
delivery_system/
  models.py              不可变值对象：SourceSegment/SourceProduct/SourceRef/
                         TranslationEntry/CoverChunk/CoverDiagnosis/CoverResult
  source_catalog.py      源段目录（独立 SQLite + 发布快照 + release 历史）
                         + PublishedSourceReader（交付侧只读句柄）
  coverage.py            条目评估 + 精确覆盖区间 DP + 失败诊断
  delivery_repository.py 译文条目、覆盖计划、不可变发布包（独立 SQLite）
  package_io.py          translation-package/1 写入与结构/覆盖校验
  rights_models.py       权属/票据值对象：BlockRef/Scope/UsageRegistration/
                         RootGrant/Delegation/ChainPath/SigningAnswer/Ticket/
                         RequirementAtom/Credential/TicketPlan
  rights_engine.py       纯逻辑：委托链 DFS（逐级取交集、可再转授、撤销剪枝）
                         + 互斥报价组下的最小费用集合覆盖（分支定界）
  rights_repository.py   权属仓（第三个独立 SQLite）：登记/原始授权/委托/撤销/票
  clearance.py           从实际译包取译块，驱动权属问答与票据规划
  rights_demo.py         实际译包→权属→票据端到端复现（含 3→4→2 算例）
  cli.py / __main__.py   命令行（含 `rights-demo`）
  demo.py                materials/753 端到端复现（调用官方 package_reader.py）
tests/test_delivery.py   20 个测试：覆盖、过期、重组、不可变、规模
tests/test_rights.py     32 个测试：范围交集、委托链、撤销、独立路径、票据/互斥组
```

## 运行权属与票据演示

```bash
python3 -m delivery_system rights-demo     # 实际译包 → 委托链 → 最小费用票据
```

## 权属席与发行席（在实际译包之上）

### 实际译块身份（不靠文本）
- `BlockRef` 由**译条号 + 全部 `(segment_id, revision)` 源段修订**组成。同一文字
  换成另一译条（不同 strip id）就是另一块，`BlockRef.matches` 不通过，**无法冒用**
  原块的用稿登记；规划直接以 `unregistered_blocks` 拒售，绝不写成已获许可。
- 一个实际译块可挂**多项**用稿登记（多个权利方各列一项或多项原始用稿权），引擎按
  块取全部登记，登记列出的每个权利都必须覆盖。

### 委托链（逐级收窄，不做扁平化缓存）
- 原始 `RootGrant` 绑定某项登记的一个权利；`Delegation` 列明上一份授权、受托方、
  范围与是否可再转授。关系落库，每次问答都用 `RightsEngine` 重新走链。
- `Scope`（场地/语种/含首尾展日区间/用途）在每一跳取**交集**；交集为空的跳不形成
  有效路径。`不能再转授` 者之下的整层委托判为无效（`explain_delegation` 给出原因）。
- 撤销一份委托只置标记，重建链时**只剪掉经过它的路径**；同一权利可经多条独立路径
  抵达不同签发商，互不影响。
- `can_sign(商, 文档, 块, 权利, 场地/语种/展日/用途)` 单独回答能否签字，返回
  `chains`（从原权利人到该商的有效链）与 `other_sources`（其他仍可用的签发来源，
  含原权利人本人的零跳链）。
- **无权属资料的旧书**（`legacy_document_ids` 或全书无任何登记）照旧可读：不产生
  清关需求、零费用通过。

### 发行票据（整票一次费 + 互斥组 + 只认真能签的权利）
- 每张 `Ticket` 有商号、明列权利、适用展日、整票费、互斥报价组（一组最多买一张）。
- 票对某 `(块, 展日, 权利)` 格计权，当且仅当该商此刻**确实持有有效链**；票面多列但
  签不了的权利自动剔除。整票只付一次，不按盖了几块计价（季票可盖几十项）。
- 定好书/语种/场地/若干展日后，每个实播块每个展日的全部用稿权必须覆盖，允许多票
  合用。求解是带互斥组约束的**精确分支定界最小费用集合覆盖**（含按掩码记忆化与可
  采纳下界剪枝），已对随机案例与暴力枚举逐一核对最优解。
- 输出 `TicketPlan`：最低总价、所选票、以及**逐日逐块逐权利的 `Credential`**，每条
  凭据附带从原权利人起的完整有效链。无票可覆盖时返回 `ok=False` 与
  `uncovered_requirements`，拒售场次不会被写成已获许可。

### 内置算例（rights_demo，单日两块各需 P、Q）
| 情形 | 选择 | 总价 |
|---|---|---|
| 甲整票(持 P、Q 两份独立委托) vs 乙P/丙Q 散票各 2 | 甲整票 | 3 |
| 撤销甲的 Q 委托后，甲票只对 P 有效 | 乙P + 丙Q 两张散票 | 4 |
| 授权不变，仅把乙、丙散票调到各 1 | 乙P + 丙Q | 2 |
| 移除丙票（Q 再无签发商） | 拒售 `uncovered_requirements` | — |

另有独立路径演示：撤销通往戊的委托后，同一权利通往丁的另一条独立委托仍有效；
以及同文异条冒用被拒、不可再转授孙链失效、范围出窗失效、旧书免清关等场景。

## 两侧的实质逻辑

### 源段目录（写入侧 + 只读快照）
- `segment_id` 是身份；`position` 只是当前序位，重排只改位置不改身份/修订。
- 每段有独立 `revision`。`revise_segment` 只增修订、不改旧修订号——不存在
  “把旧译文贴上 2 号修订”这一动作的入口。
- 每次结构性变更（建/增/删/改/移）都写一条 `source_releases` 快照，
  `latest_release_id` / `product_at_release` 可回溯历史源状态。
- `PublishedSourceReader` 用 `file:...?mode=ro` 打开，每次调用重新读取，
  交付侧无法写源库；它读到的是**当前已发布**的源产品，因此源更正立即生效。

### 交付仓（选择侧 + 产品侧）
- 译文条目精确存储 `(segment_id, revision)`；`add_translation` 对同 id
  不同内容直接拒绝（幂等重放相同内容除外），无法改标签冒充补译。
- `assess` 把每条当前语言条目判为：
  `current_usable` / `stale_revision` / `non_contiguous` / `unknown_segment` /
  `malformed` / `empty_text` / `other_language`。**只有 `current_usable` 可入选**。
  过期/不连续/重复引用/空文本一律留作历史，不是当前候选。
- `plan_cover` 在**区间**上做可达性动态规划：起点 0 能否沿合法连续区间一步步到达 n。
  这正是不被“并集覆盖”忽悠的关键——重叠条目可能覆盖并集却无法按序不重不漏拼装。
  任意合法完整覆盖即可（不要求唯一/最少条目），按 id 字典序确定性地选一条。
- 生成时强制不变量：选中 `source_ids` 拼接必须精确等于源序；任一块文本非空；
  整体渲染文本非空。**无法交付时绝不产出空文本成功包。**

### 失败诊断（可交付性依据）
- `no_current_cover`：某些段没有任何当前可用译文，给出具体 `uncovered_segment_ids`。
- `no_exact_cover`：并集覆盖全部段，但有序精确覆盖不存在；给出
  `blocked_boundary`（装配卡住的位置）与 `crossing_translation_ids`（跨该边界、
  又不能拆开的条目），以及被排除条目及原因。FR 的 F12/F23/F4 即此例。
- `empty_source`：空源拒绝发布空包。

### 发布包（translation-package/1）
固定 `package_id/document_id/language/source_snapshot(segment_id+revision)` 及按输出顺序的
`chunks(translation_id/source_ids/text)`，另加 `source_revisions/rendered_text/
content_sha256/created_at/source_release`。包一旦写入即不可变：同 id 不同内容
抛 `PackageExistsImmutable`；旧包的原文快照与译文 id 永不改写（PACK1 保持 rev1）。

## materials/753 复现结果

| 步骤 | 结果 |
|---|---|
| EN 初始发布 PACK1 | 合法覆盖 `T1+T23+T4`（或 `T12+T3+T4`，二者皆合法） |
| FR 尝试发布 | 拒收：`no_exact_cover`，并集含四段但 F12/F23 在 S2 重叠且不可拆 |
| 更正 S2→rev2 | T12/T23/F12/F23 变 `stale_revision`；EN 缺 S2 (`no_current_cover`) |
| 补 T2_NEW(rev2) | EN 重建 `T1+T2_NEW+T3+T4`，发布 PACK2（S2@2） |
| 读旧包 PACK1 | 仍为 S1@1..S4@1 与旧译文 id/文本 |

`package_reader.py` 被以子进程方式真实读取新旧两个包并校验输出。

## 规模与不扩平台

测试覆盖 100 源段 / 5 语言 / ≥500 译文（含大量重叠诱饵），验证每语言精确覆盖、
诱饵从不入选、更正后按缺口隔离、补译后恢复。覆盖是 O(n·k) 区间 DP，
足以支撑题目规模；未引入翻译、语义评分、账号等平台能力。
