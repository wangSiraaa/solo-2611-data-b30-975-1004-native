"""清货取数规划器：在给定预算内驱动链路取证，直到封榜或明确停在续取位。

不追求理论最便宜的读取路线，只用一条可解释、可复算的贪心策略，保证：

1. 先让每册交出第一页（榜首证据 + 第一个尾最大值），这是任何上界推理的前提；
2. 只要"完全没见过的货号"的总上界仍不低于当前最好的确切领跑件数、且待翻页
   的尾最大值仍为正，就翻尾最大值最大的店——点查只能钉见过名字的货号，发现
   潜在赢家只能靠顺读，翻页同时还把该店所有未见货号的上界整体压低；
3. 尾最大值归零（再翻没有正件数行）或翻页欠费时，改用点查钉死见过但不确切、
   最可能翻盘的候选（真实存在的候选优先于只靠上界虚高者）；
4. 两类动作都做不起才返回链路的 ``unsealed`` 结论，续取页号已落盘，调用方
   top_up 后用同一轮（或重开进程恢复）接着跑。

这套规则同时适配两种链路：早市店（页贵点便宜，只翻必要的几页就转点查）与
回收库（点查很贵、整册顺读便宜，未见风险不排除就一直翻页）。
"""
from __future__ import annotations

from .clearance import HeadOfficeLink
from .errors import ClearanceError


class ClearancePlanner:
    def __init__(self, link: HeadOfficeLink):
        self.link = link

    def _affordable(self, fee: int) -> bool:
        return self.link.spent + fee <= self.link.budget

    def _best_exact_total(self) -> int | None:
        """当前已能确切求和的最大正数合计；没有确切货号时返回 None。"""
        best: int | None = None
        for sku in self.link._all_known_skus():
            if sku in self.link.suspended:
                continue
            bounds = self.link.sku_bounds(sku)
            if bounds["exact"] and bounds["lower"] > 0:
                if best is None or bounds["lower"] > best:
                    best = bounds["lower"]
        return best

    def _point_target(self) -> tuple[str, str] | None:
        """选最该钉死的非确切非暂停货号；同档按货号升序保证确定性。

        排序依据（越靠前越优先）：
        1. 下界已为正的货号——已确知真实存在的候选，优先于只靠上界虚高的货号
           （例：x 点查缺席后上界仍有 9，但它其实是零；z 已确知 8 件、上界 16，
           先钉 z 才能在有限预算内出票）；
        2. 上界高者，上界相同按下界高者，再按货号升序。
        """
        candidates = []
        for sku in self.link._all_known_skus():
            if sku in self.link.suspended:
                continue
            bounds = self.link.sku_bounds(sku)
            if bounds["exact"]:
                continue
            # 未读完且本店还没有该货号确切证据的店都可点：证据条目缺失
            # （从没见过）与 pending 上界同属未决；已读完的缺席才是确切零。
            undecided = [
                store for store in sorted(self.link.books)
                if not self.link.exhausted[store]
                and bounds["per_store"].get(store, {}).get("status") != "known"
            ]
            if undecided:
                candidates.append({
                    "sku": sku, "store": undecided[0],
                    "has_positive_lower": bounds["lower"] > 0,
                    "upper": bounds["upper"], "lower": bounds["lower"],
                })
        if not candidates:
            return None
        candidates.sort(key=lambda c: (
            0 if c["has_positive_lower"] else 1,   # 真实存在的候选第一档
            -c["upper"],                            # 同档内上界高者优先
            -c["lower"],
            c["sku"],
        ))
        chosen = candidates[0]
        return chosen["store"], chosen["sku"]

    def _max_tail_store(self) -> str | None:
        pageable = [s for s in sorted(self.link.books) if not self.link.exhausted[s]]
        if not pageable:
            return None
        pageable.sort(key=lambda s: (-(self.link.tail_max[s] or 0), s))
        return pageable[0]

    def run_until_seal(self) -> dict:
        link = self.link
        while True:
            verdict = link.try_seal()
            if verdict["status"] == "sealed":
                return verdict

            # 1. 每册第一页优先：没有尾最大值就没有任何上界推理。
            cold = [store for store in sorted(link.books)
                    if link.tail_max[store] is None and not link.exhausted[store]]
            if cold:
                if not self._affordable(link.page_fee):
                    return link.try_seal()
                link.fetch_page(cold[0])
                continue

            page_store = self._max_tail_store()
            target = self._point_target()
            leader = self._best_exact_total()
            unseen_risk = leader is None or link.unseen_upper() >= leader
            # 候选翻页店的尾最大值仍为正：下一页可能翻出新的正净数货号（潜在赢家）。
            positive_tail = page_store is not None and (link.tail_max[page_store] or 0) > 0

            # 2. 未见货号仍可能翻盘，且下一页确实还有正件数行：优先翻页。
            #    点查只能钉已见过名字的货号，发现赢家只能靠顺读；这一步同时把
            #    该店所有未见货号的上界压到新的尾最大值。
            if unseen_risk and positive_tail and self._affordable(link.page_fee):
                link.fetch_page(page_store)
                continue

            # 3. 尾最大值已归零（再翻也只是零/空）或翻页欠费：改做点查，
            #    把见过但不确切、最可能翻盘的候选钉死。
            if target is not None and self._affordable(link.point_fee):
                store, sku = target
                link.point_lookup(store, sku)
                continue

            # 4. 点查也做不起但还翻得动页：继续压界推进，避免空转。
            if page_store is not None and self._affordable(link.page_fee):
                link.fetch_page(page_store)
                continue

            # 5. 两类付费取证都做不起（或无的可做）：明确未封榜返回，绝不让
            #    链路自己抛欠费异常；续取位置已在状态文件里，追加预算即可续。
            verdict = link.try_seal()
            if verdict["status"] == "sealed":
                return verdict
            verdict["blocked_action"] = (
                "page" if page_store is not None
                else ("point" if target is not None else "none")
            )
            return verdict


def require_sealed(link: HeadOfficeLink) -> dict:
    """演示/测试用辅助：要求必须封榜，未封榜直接报错。"""
    verdict = link.try_seal()
    if verdict["status"] != "sealed":
        raise ClearanceError(f"round {link.round_id} not sealed: {verdict.get('reason')}")
    return verdict
