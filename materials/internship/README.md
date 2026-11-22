# 毕业见习配岗材料（青白场景）

两个学生 QING（青）、BAI（白），两个岗位/站点 P、Q。所有 JSON 均为 UTF-8，公开字段即合同。

## 学生侧（每人独立本地库）

| 文件 | 说明 |
| --- | --- |
| `qing_catalog_rev1.json` | 青的目录修订 1：`EXT_G→LC_G(G)`，`EXT_DUAL→LC_P(P) / LC_Q(Q)` |
| `qing_catalog_rev2_no_p.json` | 青的目录修订 2：**撤销 RD 到 P 的映射**，`EXT_DUAL` 只剩 `LC_Q` |
| `bai_catalog_rev1.json` | 白的目录修订 1（与青修订 1 同构） |
| `qing_records.json` / `bai_records.json` | 各两条三点记录：`RG`（只属 G）、`RD`（双类 P/Q） |
| `qing_requirements.json` / `bai_requirements.json` | 毕业组 `GG` 需 G 类三点 |

## 岗位与站点

| 文件 | 说明 |
| --- | --- |
| `posts.json` | 岗位 P 要 P 类三点、岗位 Q 要 Q 类三点（`internship-posts/1`） |
| `stations_open.json` | P、Q 各一席；P 接收次序 白→青，Q 接收次序 青→白 |
| `stations_p_closed.json` | P 停招（`quota=0, closed=true`），Q 留一席 |
| `preferences.json` | 青志愿 P→Q，白志愿 Q→P（各严格次序） |
| `subsidies.json` | 青→P、白→Q 为 0；两个交叉配对各为 1 |

## 三幕预期

1. 原映射：青→P、白→Q，补贴 0（两份稳定名单中最省的一份）。
2. 仅青撤销 P 映射：青失去 P，青→Q、白→P，补贴 2。
3. P 停招：Q 留青，白未安置；把白换进 Q 构成改约对。

另见 `examples/placement_demo.py` 与 `tests/test_placement.py`。双类记录不可两用的证明在测试中以单记录 P/G 双类实例给出。
