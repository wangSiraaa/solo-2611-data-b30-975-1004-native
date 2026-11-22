# gradaudit — 独立课程等价目录与毕业要求审核库

一个可外部接入的 Python 3.11 产品，包含三块相互独立、又共享同一套输入合同的组件：

| 组件 | 入口 | 作用 |
| --- | --- | --- |
| 课程等价目录 + 审核库 | `gradaudit.Store` / `python -m gradaudit ...` | SQLite 持久化目录、转入记录、要求、准备/确认/重开 |
| 精确求解器 | `gradaudit.solver.solve` / `gradaudit solve` | 判定整体可行，可行时给出具体分配；不可行时给出各组独立最大点数 |
| 独立验证器 | `gradaudit.verify.verify_plan[_document]` / `gradaudit verify` | **不调用求解器**，按输入逐条重核一个 `course-allocation/1` 方案 |
| 荣誉评优 | `gradaudit.honor` / `gradaudit honor` | 先完整分配毕业学分，再从未用记录中取整条可映射荣誉类别的记录，最大化荣誉点；输出可复核包 |
| 奖学金判定 | `gradaudit.scholarship` / `gradaudit scholarship` | 章程未定稿时逐人回答 always / some / never，并对 some 给出整届一致的定稿见证名单 |

四个公开 JSON 文档的字段含义即题面合同。目录**只**通过显式 `equivalences` 映射给出候选，绝不按课程名猜测。

材料在 `materials/725/`，演示在 `examples/demo.py`，测试在 `tests/`。

```bash
python3 -m unittest discover -s tests -v     # 31 个测试
python3 examples/demo.py                      # 全流程演示
```

---

## 1. 输入合同（公开字段）

### 目录 `course-catalog/1`
```json
{
  "format": "course-catalog/1",
  "revision": 1,
  "courses": [{"course_id": "LC_BOTH", "categories": ["CORE", "OPT"]}],
  "equivalences": {"Z": ["LC_BOTH"]}
}
```
* `courses[].categories` 是每个本地课程的类别集合。
* `equivalences[外部课程] = [本地课程ID...]` 是全部候选；一个外部课程可映射到多个本地课程。
* 候选**只能**由此映射得出；不在此映射中的外部课程没有候选。

### 转入记录
```json
{"student_id": "ST1", "revision": 1,
 "records": [{"record_id": "R1", "external_course": "X", "points": 3}]}
```
* `record_id` 稳定、唯一；`points` 为正整数（合同规模 1..6）。

### 要求
```json
{"request_id": "REQ1", "revision": 1,
 "groups": [{"group_id": "G_CORE", "allowed_categories": ["CORE"], "minimum_points": 3}]}
```
* 至多 3 组；每组给出允许类别集合与最低点数（正整数）。

### 方案 `course-allocation/1`
```json
{"format": "course-allocation/1", "student_id": "ST1",
 "catalog_revision": 1, "records_revision": 1,
 "request_id": "REQ1", "requirements_revision": 1,
 "assignments": [{"record_id": "R1", "course_id": "LC_CORE", "group_id": "G_CORE"}]}
```
* 固定身份五元组；`assignments` 每项只给 `record_id/course_id/group_id`。
* **点数一律从记录读取**，方案自报的点数字段被忽略。
* 未出现的记录按“未使用”处理；允许额外说明字段，不依赖字段顺序。

---

## 2. 问题模型

每条记录至多被选一个候选本地课程，并**完整**分配给至多一个组，不能拆点、不能跨组复用；可整体不用。选中课程至少有一个类别命中该组允许类别，才可分配到该组。各组可超额满足，解不要求唯一或最少使用记录。

* 表示层（`model.py`）把记录、候选、类别、点数全部解析成不可变对象。
* 求解器（`solver.py`）做精确判定并给出一个可行分配。
* 验证器（`verify.py`）**独立**重算：身份 → 候选 → 类别 → 点数去向 → 各组达成。任何满足合同的方案都接受，哪怕与求解器给出的不同。

---

## 3. 求解器：状态与成本

状态为 `(pointer, gains)`：

* `pointer`：下一条待决定的记录下标（记录按 `record_id` 排序，每条用一次或跳过）。所有 `< pointer` 的记录已决定，因此不需要额外的 used 位掩码。
* `gains`：各组点数，**截断到该组最低分**。截断不改变可行性，却能把“已满足同一组集合”的部分解合并成同一状态。

成本：不同 `gains` 向量数 ≤ `∏(min_g+1)`。3 组、每组最低分 ≤6 时 ≤ 343，于是状态数 ≤ `n·343`（`n ≤ 12`，约 4k）。每个状态再枚举该记录的剩余候选课程与组，即
`O(n · ∏(min+1) · (1 + Σ|候选|) · G)`。**多候选只增加转移、不增加状态；点数多样性只拓宽被截断的 gain 网格。** 这是精确 DP，可证明不可行，不是启发式。

不可行时，`groups[].max_ignoring_other_groups` 给出“忽略其它组竞争时该组能达到的最大点数”（真实总和，不截断）。例如修订 2 下 `CORE=8, OPT=2`：两组各自都够，但同一份记录不能同时计入两组，故整体不可行——单组可达**不代表**整体可行。

```bash
python3 -m gradaudit solve --catalog materials/725/catalog.json \
  --records materials/725/records.json --requirements materials/725/requirements.json \
  --emit-plan /tmp/plan.json          # 退出码 0=可行 1=不可行
```

---

## 4. 独立验证入口（verify-plan）

```bash
python3 -m gradaudit verify --plan materials/725/plan_a.json \
  --catalog materials/725/catalog.json \
  --records materials/725/records.json \
  --requirements materials/725/requirements.json
```

`verify` **不导入求解器**，也不与求解器选中的方案逐项比对。它按输入重新核：

| 情形 | 结果码 |
| --- | --- |
| 方案身份/修订与输入不符 | `identity_mismatch` / `revision_mismatch` |
| 记录不存在 | `unknown_record`（定位到 record_id） |
| 同一 record 出现多次（复用/拆点） | `duplicate_record`（定位到 record_id） |
| 课程不是该外部课程的候选 | `course_not_candidate` |
| 课程类别不命中该组 | `category_mismatch` |
| 组点数不足 | `group_underfilled`（带 achieved/minimum） |
| 未知组 | `unknown_group` |

退出码：`0` 接受，`1` 拒绝，`2` 输入错误。任何满足全部合同的方案都接受；缺省记录按未使用。

**两份由题面独立给定、分配不同的合法方案**（`plan_a`、`plan_b`）都被接受；`plan_invalid_reuse` 复用 R1，被拒绝且原因定位到 `R1`。新生成的方案（`solve --emit-plan`）也走同一入口并接受。

---

## 5. 审核库：准备 / 确认 / 重开

```python
from gradaudit import Store
store = Store("gradaudit.sqlite3")
store.import_catalog(doc); store.import_records(doc); store.import_requirements(doc)

prep = store.prepare_audit(plan_doc, request_id="REQ1",
                           catalog_revision=1, records_revision=1, requirements_revision=1)
store.confirm_audit(request_id="REQ1")
store.confirmation_status("REQ1")      # -> confirmed / stale
store.reopen_audit(request_id="REQ1", reason="...")
store.history("REQ1")
```

### 修订与导入
* 每个目录 / 记录 / 要求修订都以 **内容哈希 + 原文** 落库。
* 相同修订号、不同内容再次导入 → `RevisionConflictError`（**拒绝**，不得把新映射冒充旧修订）；相同内容重复导入幂等。
* 旧修订始终可查（`get_catalog(1)`、`list_candidates(1, "X")` 等）；映射修订真实改变下一次可行性（修订 2 后 OPT 只能取 2 点，整体不可行）。

### 准备（prepare）
* 固定当时的 `(catalog, records, requirements)` 修订。
* 用**同一个独立验证器**核方案（不限求解器产物，外部方案同样走身份核对与确认边界），并持久化每条记录的**点数去向**（`preparation_points`）。
* 被拒方案也会作为“已拒绝的准备”留痕，且永远不能确认。

### 确认边界
* 只有被独立验证器**接受**的准备才能确认。
* 确认时要求“当前有效修订”仍等于准备时固定的修订。当前有效修订取：最新目录修订、该请求最新要求修订、该学生最新记录修订。**只要准备后目录 / 要求 / 学生记录发生变化，旧方案即 `stale`，不能继续确认**，必须重新准备（`ConfirmationStaleError`）。
* 确认**不删除**任何原记录：它只新增确认行，保留原准备、原方案、使用的修订。
* 重开（reopen）记录显式事件、清除“当前确认”状态，但全部历史（准备、确认、事件）保留，可回查所有点数去向。

### 主要表
`catalog_revisions / catalog_courses / catalog_course_categories / catalog_equivalences`、
`records_revisions / transfer_records`、
`requirements_revisions / requirement_groups / requirement_group_categories`、
`audit_requests`、`audit_preparations / preparation_points`、
`audit_confirmations`、`audit_events`。

---

## 6. 目录维护

```bash
python3 -m gradaudit import-catalog materials/725/catalog.json
python3 -m gradaudit import-catalog materials/725/catalog_revision2.json   # 修订 2
python3 -m gradaudit list-candidates 1 X    # 独立列出候选及类别，不替学生选择
```

---

## 7. 荣誉评优（graduation 先行，荣誉取剩余）

给定一名学生、一份**在册**（enrolled）毕业要求与一份章程草案，`gradaudit.honor` 先完整分配其毕业学分，再只在**未分给毕业**的记录里挑选整条可映射到荣誉类别的记录，使荣誉点之和最大：

* 记录不可拆、不可兼用：同一条记录不能既计入毕业又计入荣誉（`record_shared` 会被验证器拒绝）。
* 若根本不能毕业，结果为 `eligible=false`、`honor_points=null`（无资格），验证器用精确求解器确认确实不可行。
* 为最大化荣誉点**允许不同于求解器默认产物的合法毕业分配**；只要求毕业可行。

```python
from gradaudit.honor import evaluate_honor, review_bundle, apply_choices, CharterDraft

res = evaluate_honor(catalog, records, requirements, ["HONOR"])           # 章程未覆盖，候选自由
res = evaluate_honor(cat, recs, req, ["HONOR"], fixed_choices={"E": "E_HON"})  # 固定某码选择
bundle = review_bundle(catalog, records, requirements, ["HONOR"], choices={"E": "E_HON"})
```

章程与候选约束：

* `CharterDraft.from_catalog(catalog, covered_codes)` 只为目录里确有其候选的外部码建章程；**章程不能引入目录没有的候选**。
* `apply_choices(catalog, choices)` 把被覆盖码的候选集收成唯一一个；每码的选择必须是该码的目录候选，否则 `SchemaError`。未被覆盖的码照旧目录。
* 同一外部码在**整批学生、全部出现位置**都用同一选择。
* `review_bundle` 输出 `format=honor-review/1`：固定 `student_id` 与三个修订及内容哈希，附 `fingerprint`，毕业分配再用**普通独立验证器**复核、荣誉选择用 `verify_honor_result` 复核，校内可直接重跑。

```bash
python3 -m gradaudit honor --catalog cat.json --records recs.json \
  --requirements req.json --honor-category HONOR --choices choices.json
```

---

## 8. 奖学金判定（章程未定稿时的逐人答复）

`gradaudit.scholarship.assess_scholarship` 在章程的全部**合法定稿**（每个被覆盖码取目录候选之一）上枚举，逐人回答：

| 结论 | 含义 |
| --- | --- |
| `always` | 在**每一种**合法定稿下都能获奖 |
| `some` | 仅在**某些**定稿下获奖（结论会变） |
| `never` | 任何定稿下都不可能获奖 |

对 `some` 学生给出两份**整届一致**的见证定稿：`available_under`（此时获奖）与 `not_available_under`（此时不获奖），每份都附**在该单一整届定稿下重算**的完整获奖名单。**绝不**把某生在甲定稿的分数与另一生在乙定稿的分数拼成排名。

* 名额与同分次序由奖学金办公室给定；同分次序即 `tie_order`（未显式列出者按 `student_id` 排到末尾）。
* 领取名单永远按**同一份**确定性定稿签发：`canonical_finalization`（被覆盖码按码排序各取第一候选）。`Store.issue_award_list(report)` 只接受由 `assess_scholarship` 产出的报告（带 `read_only` 标记），手工改过的排名不能冒充名单签发。
* 逐生核分与试算**只读**：`Store.assess_scholarship` 从不 prepare / confirm / reopen，原有毕业确认不受试算影响（`confirmation_id` 不变）。每份报告固定每个学生的 `records_revision/hash`、`requirements_revision/hash` 与 `catalog_revision`，把修订与学生身份一起固定。

```bash
# batch.json: {"entries":[{records,requirements}...],"covered_codes":[...],
#              "honor_categories":[...],"quota":N,"tie_order":[...]}
python3 -m gradaudit scholarship --catalog cat.json --batch batch.json
```

### 题面场景（三人、两名额、同分次序 C、B、A）

三人各有独占 G 类 3 点可毕业；A 另有专属荣誉 3 点与 E 2 点，B 另有专属荣誉 2 点与 E 2 点，C 另有专属荣誉 4 点；E 可解释为荣誉或普通选修。

* **E 任选**：A 荣誉 3+2、C 荣誉 4、B 荣誉 2+2 或 2；两席里 A 恒在 → A、C 为 `always`，B 为 `never`。
* **把 B 的 E 记录更正为独立码 F**（E、F 各可两选）：当 E 判普通、F 判荣誉时 B 得 4 点压过 A 的 3 点，A 落到第三 → A 变为 `some`；见证名单：`available_under={E:E_HON,F:F_HON}` 奖励 A、C；`not_available_under={E:E_ORD,F:F_HON}` 奖励 C、B。
* **恢复原记录、仅把名额收为一**：A 与 C 争唯一席位，A 依同分次序在 C 之后 → A 变为 `some`，B 仍 `never`。

可复现演示：`python3 examples/scholarship_demo.py` 逐场打印上述三种情形的 always/some/never 与整届一致的见证获奖名单。

---

## 9. 测试覆盖

`tests/test_gradaudit.py`（51 项）在原 31 项基础上新增：荣誉须先完整毕业、不可毕业即无资格、荣誉 DP 与暴力枚举最优性交叉校验、章程选择必须是目录候选且不能引入新候选、章程改写改变荣誉结果、验证器拒绝超额/兼用/虚假无资格；奖学金 always/some/never、见证名单整届一致且不混定稿、never 无见证、未知同分次序被拒；`Store` 试算只读不改原确认、名单按规范定稿签发、拒绝手改报告、学生与修订归属校验。

规模合同：至 12 份记录、每份至多 3 候选、点数 1..6，均在 `solver.py` 中显式校验。不做排课、GPA 或先修。
