"""Tests for physical double-sided coupon paper and joint basket redemption.

Every expectation is derived from the brief, not from the service's internals:
the worked two-cell example (12 / 14 / 7), a tear that needs a vertical cut
before a horizontal one, two counters racing for one sheet, receipt-time
perforation corrections, and refunds from each sale's original per-unit paid
amounts.
"""

from __future__ import annotations

import itertools
import random
import unittest
from collections import Counter

from basket_service import open_service
from basket_service.errors import BasketError
from basket_service.reference import exhaustive_best
from basket_service.validation import validate_basket, validate_coupons


def two_cell_tickets():
    """Front: left A -> X-4, right B -> Y-4.  Back: whole C -> X-6."""

    return [
        {"ticket_id": "A", "side": "front", "rect": [0, 0, 0, 0], "coupon_id": "A", "D": 4, "requires": {"X": 1}},
        {"ticket_id": "B", "side": "front", "rect": [0, 1, 0, 1], "coupon_id": "B", "D": 4, "requires": {"Y": 1}},
        {"ticket_id": "C", "side": "back", "rect": [0, 0, 0, 1], "coupon_id": "C", "D": 6, "requires": {"X": 1}},
    ]


def pair_baskets(y_price=10):
    return [
        {"basket_id": "jia", "units": [{"unit_id": "jx", "sku": "X", "price": 10}]},
        {"basket_id": "yi", "units": [{"unit_id": "yy", "sku": "Y", "price": y_price}]},
    ]


def quasi_brute_group(baskets, coupons, copies):
    """Independent optimum: per-basket brute subsets, cap by physical copies."""

    units = [validate_basket(b["units"]) for b in baskets]
    all_skus = set().union(*[{u["sku"] for u in uu} for uu in units])
    canonical = validate_coupons([c for c in coupons if set(c["requires"]) <= all_skus], all_skus)
    by_id = {c["coupon_id"]: c for c in canonical}
    options = []
    for uu in units:
        skus = {u["sku"] for u in uu}
        feasible = [c["coupon_id"] for c in canonical if set(c["requires"]) <= skus]
        table = {}
        for size in range(len(feasible) + 1):
            for combo in itertools.combinations(feasible, size):
                subset = [dict(by_id[cid]) for cid in combo]
                total, used, _, _ = exhaustive_best(uu, subset)
                if set(used) == set(combo):
                    table[combo] = total
        options.append(table)
    best = None
    for picks in itertools.product(*[list(table.items()) for table in options]):
        counts = Counter()
        for combo, _ in picks:
            counts.update(combo)
        if any(counts[cid] > copies.get(cid, 0) for cid in counts):
            continue
        total = sum(value for _, value in picks)
        if best is None or total < best:
            best = total
    return best


class TwoCellExampleTests(unittest.TestCase):
    def test_punched_middle_splits_into_two_tickets_total_12(self):
        svc = open_service(":memory:")
        svc.register_sheet("s1", rows=1, cols=2, perforations=["V0,0"], tickets=two_cell_tickets())
        trial = svc.try_tear("s1", ["A", "B"])
        self.assertEqual(len(trial["steps"]), 1)
        self.assertEqual(trial["steps"][0]["kind"], "V")
        quote = svc.quote_group("g1", pair_baskets(), accepted_coupons=["A", "B", "C"])
        self.assertEqual(quote["total_paid"], 12)
        self.assertEqual({b["basket_id"]: b["coupon_ids"] for b in quote["baskets"]}, {"jia": ["A"], "yi": ["B"]})

    def test_unpunched_middle_only_whole_back_ticket_total_14(self):
        svc = open_service(":memory:")
        svc.register_sheet("s1", rows=1, cols=2, perforations=[], tickets=two_cell_tickets())
        with self.assertRaises(BasketError) as ctx:
            svc.try_tear("s1", ["A", "B"])
        self.assertEqual(ctx.exception.code, "TEAR_INFEASIBLE")
        quote = svc.quote_group("g1", pair_baskets(), accepted_coupons=["A", "B", "C"])
        # C (whole piece, X-6) goes to Jia; Yi stays full price.
        self.assertEqual(quote["total_paid"], 14)
        self.assertEqual({b["basket_id"]: b["coupon_ids"] for b in quote["baskets"]}, {"jia": ["C"], "yi": []})

    def test_cheap_y_makes_b_unusable_and_whole_piece_total_7(self):
        svc = open_service(":memory:")
        svc.register_sheet("s1", rows=1, cols=2, perforations=[], tickets=two_cell_tickets())
        # B (Y-4) cannot cover a 3-cent Y unit, so only the whole-piece C applies.
        quote = svc.quote_group("g1", pair_baskets(y_price=3), accepted_coupons=["A", "B", "C"])
        self.assertEqual(quote["total_paid"], 7)
        self.assertEqual({b["basket_id"]: b["coupon_ids"] for b in quote["baskets"]}, {"jia": ["C"], "yi": []})

    def test_punched_can_beat_whole_piece_when_b_usable(self):
        svc = open_service(":memory:")
        svc.register_sheet("s1", rows=1, cols=2, perforations=["V0,0"], tickets=two_cell_tickets())
        quote = svc.quote_group("g1", pair_baskets(y_price=5), accepted_coupons=["A", "B", "C"])
        # A+B beats C: (10+5)-8=7 vs (10+5)-6=9.
        self.assertEqual(quote["total_paid"], 7)


class TearingTests(unittest.TestCase):
    def test_requires_vertical_then_horizontal(self):
        # 2x2 sheet; isolating bottom-right (1,1) needs a vertical cut through
        # both rows, then a horizontal cut on the right column.
        tickets = [
            {"ticket_id": "BR", "side": "front", "rect": [1, 1, 1, 1], "coupon_id": "BR", "D": 3, "requires": {"X": 1}},
            {"ticket_id": "BL", "side": "front", "rect": [1, 0, 1, 0], "coupon_id": "BL", "D": 3, "requires": {"X": 1}},
            {"ticket_id": "TR", "side": "front", "rect": [0, 1, 0, 1], "coupon_id": "TR", "D": 3, "requires": {"X": 1}},
            {"ticket_id": "TL", "side": "front", "rect": [0, 0, 0, 0], "coupon_id": "TL", "D": 3, "requires": {"X": 1}},
        ]
        svc = open_service(":memory:")
        svc.register_sheet("s", rows=2, cols=2, perforations=["V0,0", "V1,0", "H0,1"], tickets=tickets)
        trial = svc.try_tear("s", ["BR"])
        kinds = [step["kind"] for step in trial["steps"]]
        self.assertEqual(kinds, ["V", "H"])
        self.assertEqual(trial["projected_remaining"], [[0, 0, 1, 0], [0, 1, 0, 1]])

    def test_missing_punch_blocks_then_correction_unblocks(self):
        # A horizontal cut at row boundary 0 needs H0,0 and H0,1.  The craft
        # record misses H0,1, so the two-row sheet cannot be split at first.
        tickets = [
            {"ticket_id": "top", "side": "front", "rect": [0, 0, 0, 1], "coupon_id": "top", "D": 2, "requires": {"X": 1}},
            {"ticket_id": "bot", "side": "front", "rect": [1, 0, 1, 1], "coupon_id": "bot", "D": 2, "requires": {"X": 1}},
        ]
        svc = open_service(":memory:")
        svc.register_sheet("s", rows=2, cols=2, perforations=["H0,0"], tickets=tickets)
        with self.assertRaises(BasketError) as ctx:
            svc.try_tear("s", ["bot"])
        self.assertEqual(ctx.exception.code, "TEAR_INFEASIBLE")
        fixed = svc.add_correction("fix1", "s", ["H0,1"])
        self.assertIn("H0,1", fixed["effective_perforations"])
        trial = svc.try_tear("s", ["bot"])
        self.assertEqual(len(trial["steps"]), 1)

    def test_unused_corners_may_be_discarded(self):
        # Taking the top row leaves the bottom row as discardable remainder.
        tickets = [
            {"ticket_id": "top", "side": "front", "rect": [0, 0, 0, 1], "coupon_id": "top", "D": 2, "requires": {"X": 1}},
        ]
        svc = open_service(":memory:")
        svc.register_sheet("s", rows=2, cols=2, perforations=["H0,0", "H0,1"], tickets=tickets)
        trial = svc.try_tear("s", ["top"])
        self.assertEqual(trial["projected_remaining"], [[1, 0, 1, 1]])

    def test_two_tickets_on_one_rectangle_cannot_both_be_taken(self):
        tickets = [
            {"ticket_id": "A", "side": "front", "rect": [0, 0, 0, 1], "coupon_id": "A", "D": 5, "requires": {"X": 1}},
            {"ticket_id": "C", "side": "back", "rect": [0, 0, 0, 1], "coupon_id": "C", "D": 6, "requires": {"X": 1}},
        ]
        svc = open_service(":memory:")
        svc.register_sheet("s", rows=1, cols=2, tickets=tickets)
        with self.assertRaises(BasketError):
            svc.try_tear("s", ["A", "C"])
        # Each single ticket is the untouched whole piece.
        self.assertEqual(svc.try_tear("s", ["A"])["steps"], [])
        self.assertEqual(svc.try_tear("s", ["C"])["steps"], [])


class GroupTests(unittest.TestCase):
    def test_commit_moves_paper_and_registers_original_quotes(self):
        svc = open_service(":memory:")
        svc.register_sheet("s1", rows=1, cols=2, perforations=["V0,0"], tickets=two_cell_tickets())
        trial = svc.quote_group("g1", pair_baskets(), request_id="req1")
        self.assertEqual(trial["total_paid"], 12)
        result = svc.commit_group("req1", {"jia": "S-jia", "yi": "S-yi"})
        self.assertEqual(result["sheets"], {"s1": []})
        jia = svc.get_sale("S-jia")
        yi = svc.get_sale("S-yi")
        self.assertEqual(jia["total"], 6)
        self.assertEqual(yi["total"], 6)
        # Refund follows each sale's original per-unit paid amount.
        self.assertEqual(svc.return_units("r-jia", "S-jia", ["jx"], {"jx": "RESTOCK"})["refund_total"], jia["units"][0]["paid"])
        self.assertEqual(svc.return_units("r-yi", "S-yi", ["yy"], {"yy": "SCRAP"})["refund_total"], yi["units"][0]["paid"])

    def test_trial_does_not_move_paper(self):
        svc = open_service(":memory:")
        svc.register_sheet("s1", rows=1, cols=2, perforations=["V0,0"], tickets=two_cell_tickets())
        before = svc.get_sheet("s1")["pieces"]
        svc.quote_group("g1", pair_baskets(), request_id="req1")
        svc.try_tear("s1", ["A", "B"])
        self.assertEqual(svc.get_sheet("s1")["pieces"], before)

    def test_same_sheet_competition_only_one_commit_wins(self):
        svc = open_service(":memory:")
        svc.register_sheet("s1", rows=1, cols=2, perforations=["V0,0"], tickets=two_cell_tickets())
        svc.quote_group("ga", pair_baskets(), request_id="qa")
        svc.quote_group("gb", pair_baskets(), request_id="qb")
        svc.commit_group("qa", {"jia": "S-jia", "yi": "S-yi"})
        with self.assertRaises(BasketError) as ctx:
            svc.commit_group("qb", {"jia": "S2-jia", "yi": "S2-yi"})
        self.assertEqual(ctx.exception.code, "PAPER_CONFLICT")
        # Nothing from the losing group leaked into the ledger.
        self.assertFalse(any(s["sale_id"] == "S2-jia" for s in svc.audit_sales()))

    def test_concurrent_commits_on_one_sheet(self):
        import tempfile
        import threading
        import time
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "race.db")
            setup = open_service(path)
            setup.register_sheet("s1", rows=1, cols=2, perforations=["V0,0"], tickets=two_cell_tickets())
            setup.quote_group("g1", pair_baskets(), request_id="qa")
            setup.quote_group("g1", pair_baskets(), request_id="qb")
            del setup

            barrier = threading.Barrier(2)
            successes, failures, lock = [], [], threading.Lock()

            def worker(request_id, suffix):
                client = open_service(path)
                try:
                    barrier.wait()
                    result = client.commit_group(request_id, {"jia": f"S{suffix}-jia", "yi": f"S{suffix}-yi"})
                    with lock:
                        successes.append(request_id)
                except BasketError as exc:
                    with lock:
                        failures.append(exc.code)

            threads = [threading.Thread(target=worker, args=(req, i)) for i, req in enumerate(["qa", "qb"])]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(len(successes), 1)
            self.assertEqual(failures, ["PAPER_CONFLICT"])
            final = open_service(path)
            self.assertEqual(final.get_sheet("s1")["pieces"], [])
            self.assertEqual(len(final.audit_sales()), 2)

    def test_one_copy_two_baskets(self):
        # A single whole-piece ticket can back only one basket; the other pays
        # full price.  Ordering gives the discount to the first basket id.
        tickets = [
            {"ticket_id": "A", "side": "front", "rect": [0, 0, 0, 1], "coupon_id": "A", "D": 4, "requires": {"X": 1}}
        ]
        svc = open_service(":memory:")
        svc.register_sheet("s", rows=1, cols=2, tickets=tickets)
        baskets = [
            {"basket_id": "b1", "units": [{"unit_id": "x1", "sku": "X", "price": 10}]},
            {"basket_id": "b2", "units": [{"unit_id": "x2", "sku": "X", "price": 10}]},
        ]
        quote = svc.quote_group("g", baskets, accepted_coupons=["A"], store=False)
        self.assertEqual(quote["total_paid"], 16)
        self.assertEqual({b["basket_id"]: b["coupon_ids"] for b in quote["baskets"]}, {"b1": ["A"], "b2": []})

    def test_two_compatible_counters_both_commit(self):
        # Two trials take physically disjoint tickets (A's left cell and B's
        # right cell); each must be able to hand over its piece independently.
        svc = open_service(":memory:")
        svc.register_sheet("s1", rows=1, cols=2, perforations=["V0,0"], tickets=two_cell_tickets())
        svc.quote_group("ga", [{"basket_id": "jia", "units": [{"unit_id": "jx", "sku": "X", "price": 10}]}], request_id="qa", accepted_coupons=["A"])
        svc.quote_group("gb", [{"basket_id": "yi", "units": [{"unit_id": "yy", "sku": "Y", "price": 10}]}], request_id="qb", accepted_coupons=["B"])
        svc.commit_group("qa", {"jia": "S-jia"})
        svc.commit_group("qb", {"yi": "S-yi"})
        self.assertEqual(svc.get_sheet("s1")["pieces"], [])
        self.assertEqual({s["sale_id"] for s in svc.audit_sales()}, {"S-jia", "S-yi"})

    def test_partial_handover_leaves_only_true_remainder(self):
        # Force A (left cell) to be taken first; the right cell stays usable for
        # B, while the whole-piece C is physically gone and can no longer help.
        svc = open_service(":memory:")
        svc.register_sheet("s1", rows=1, cols=2, perforations=["V0,0"], tickets=two_cell_tickets())
        svc.quote_group("gA", [{"basket_id": "jia", "units": [{"unit_id": "jx", "sku": "X", "price": 10}]}], request_id="qA", accepted_coupons=["A"])
        svc.commit_group("qA", {"jia": "S-jia"})
        self.assertEqual(svc.get_sheet("s1")["pieces"], [[0, 1, 0, 1]])
        self.assertEqual([t["ticket_id"] for t in svc.get_sheet("s1")["remaining_tickets"]], ["B"])
        # B is still takeable from the remainder.
        svc.quote_group("gB", [{"basket_id": "yi", "units": [{"unit_id": "yy", "sku": "Y", "price": 10}]}], request_id="qB", accepted_coupons=["B"])
        svc.commit_group("qB", {"yi": "S-yi"})
        self.assertEqual(svc.get_sheet("s1")["pieces"], [])
        # C is no longer on paper: a new basket simply pays full price.
        q = svc.quote_group("gC", [{"basket_id": "j2", "units": [{"unit_id": "j2x", "sku": "X", "price": 10}]}], request_id="qC", accepted_coupons=["C"], store=False)
        self.assertEqual(q["total_paid"], 10)
        self.assertEqual(q["handovers"], [])

    def test_one_promotion_at_most_once_per_basket(self):
        # Two physical copies of the same A on separate sheets; a single-unit
        # basket may still use A only once.
        tickets = [{"ticket_id": "A", "side": "front", "rect": [0, 0, 0, 0], "coupon_id": "A", "D": 4, "requires": {"X": 1}}]
        svc = open_service(":memory:")
        svc.register_sheet("s1", rows=1, cols=1, tickets=tickets)
        svc.register_sheet("s2", rows=1, cols=1, tickets=[dict(tickets[0], ticket_id="A2")])
        baskets = [{"basket_id": "b1", "units": [{"unit_id": "x1", "sku": "X", "price": 10}]}]
        quote = svc.quote_group("g", baskets, accepted_coupons=["A"], store=False)
        self.assertEqual(quote["baskets"][0]["coupon_ids"], ["A"])
        self.assertEqual(quote["total_paid"], 6)

    def test_accepted_catalogue_can_be_smaller_than_paper(self):
        # Paper prints A, B, C but the merchant only honours B.
        svc = open_service(":memory:")
        svc.register_sheet("s1", rows=1, cols=2, perforations=["V0,0"], tickets=two_cell_tickets())
        quote = svc.quote_group("g1", pair_baskets(), accepted_coupons=["B"])
        self.assertEqual(quote["total_paid"], 16)  # only Yi's B applies
        self.assertEqual({b["basket_id"]: b["coupon_ids"] for b in quote["baskets"]}, {"jia": [], "yi": ["B"]})

    def test_empty_accepted_catalogue_pays_full_price(self):
        svc = open_service(":memory:")
        svc.register_sheet("s1", rows=1, cols=2, perforations=["V0,0"], tickets=two_cell_tickets())
        quote = svc.quote_group("g1", pair_baskets(), accepted_coupons=[])
        self.assertEqual(quote["total_paid"], 20)
        self.assertTrue(all(b["coupon_ids"] == [] for b in quote["baskets"]))

    def test_group_quote_replay_and_conflict(self):
        svc = open_service(":memory:")
        svc.register_sheet("s1", rows=1, cols=2, perforations=["V0,0"], tickets=two_cell_tickets())
        first = svc.quote_group("g1", pair_baskets(), request_id="req")
        again = svc.quote_group("g1", pair_baskets(), request_id="req")
        self.assertEqual(first, again)
        with self.assertRaises(BasketError) as ctx:
            svc.quote_group("g1", pair_baskets(y_price=8), request_id="req")
        self.assertEqual(ctx.exception.code, "GROUP_CONFLICT")

    def test_commit_is_idempotent(self):
        svc = open_service(":memory:")
        svc.register_sheet("s1", rows=1, cols=2, perforations=["V0,0"], tickets=two_cell_tickets())
        svc.quote_group("g1", pair_baskets(), request_id="req")
        first = svc.commit_group("req", {"jia": "S-jia", "yi": "S-yi"})
        again = svc.commit_group("req", {"jia": "S-jia", "yi": "S-yi"})
        self.assertFalse(first["repeated"])
        self.assertTrue(again["repeated"])
        with self.assertRaises(BasketError) as ctx:
            svc.commit_group("req", {"jia": "OTHER", "yi": "S-yi"})
        self.assertEqual(ctx.exception.code, "GROUP_CONFLICT")

    def test_sale_survives_snapshot_switch(self):
        svc = open_service(":memory:")
        tickets = [{"ticket_id": "A", "side": "front", "rect": [0, 0, 0, 0], "coupon_id": "A", "D": 4, "requires": {"X": 1}}]
        svc.register_sheet("s", rows=1, cols=1, tickets=tickets)
        baskets = [{"basket_id": "b1", "units": [{"unit_id": "x1", "sku": "X", "price": 10}]}]
        svc.quote_group("g", baskets, request_id="rq")
        svc.commit_group("rq", {"b1": "S1"})
        before = svc.get_sale("S1")
        svc.publish_snapshot([{"sku": "X", "price": 5}], [{"coupon_id": "A", "D": 9, "requires": {"X": 1}}], snapshot_id="new")
        self.assertEqual(svc.get_sale("S1"), before)
        self.assertEqual(svc.return_units("rr", "S1", ["x1"], {"x1": "RESTOCK"})["refund_total"], before["units"][0]["paid"])

    def test_reopen_preserves_paper_and_sales(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "paper.db")
            svc = open_service(path)
            svc.register_sheet("s1", rows=1, cols=2, perforations=["V0,0"], tickets=two_cell_tickets())
            svc.quote_group("g1", pair_baskets(), request_id="req")
            svc.commit_group("req", {"jia": "S-jia", "yi": "S-yi"})
            del svc
            reopened = open_service(path)
            self.assertEqual(reopened.get_sheet("s1")["pieces"], [])
            self.assertEqual(reopened.get_sale("S-jia")["total"], 6)
            self.assertEqual(reopened.return_units("r", "S-yi", ["yy"], {"yy": "RESTOCK"})["refund_total"], 6)


class PlanRunnerTests(unittest.TestCase):
    def test_paper_plan_round_trip(self):
        plan = {
            "steps": [
                {
                    "op": "register_sheet",
                    "sheet_id": "s1",
                    "rows": 1,
                    "cols": 2,
                    "perforations": ["V0,0"],
                    "tickets": two_cell_tickets(),
                },
                {"name": "tear", "op": "try_tear", "sheet_id": "s1", "ticket_ids": ["A", "B"]},
                {"name": "g", "op": "quote_group", "group_id": "g1", "request_id": "rq", "baskets": pair_baskets()},
                {"name": "c", "op": "commit_group", "request_id": "rq", "sales": {"jia": "S-jia", "yi": "S-yi"}},
                {"op": "get_sheet", "sheet_id": "s1"},
            ]
        }
        svc = open_service(":memory:")
        result = svc.run(plan)
        self.assertTrue(result["ok"])
        self.assertEqual(result["steps"][2]["result"]["total_paid"], 12)
        self.assertEqual(result["steps"][4]["result"]["pieces"], [])


class TearingDifferentialTests(unittest.TestCase):
    """Independent BFS over paper partitions vs. the service's tear planner."""

    @staticmethod
    def _can_split(piece, kind, at, edges):
        from basket_service.sheets import cut_edges

        return all(e in edges for e in cut_edges(piece, kind, at))

    def _reachable(self, rows, cols, edges, targets):
        from basket_service.sheets import rects_disjoint, split_rect, whole_sheet_rect

        wanted = frozenset(targets)
        items = list(wanted)
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if not rects_disjoint(items[i], items[j]):
                    return False
        seen, stack = set(), [frozenset([whole_sheet_rect(rows, cols)])]
        while stack:
            pieces = stack.pop()
            if pieces in seen:
                continue
            seen.add(pieces)
            if wanted.issubset(pieces):
                return True
            for piece in pieces:
                r0, c0, r1, c1 = piece
                for at in range(r0, r1):
                    if self._can_split(piece, "H", at, edges):
                        left, right = split_rect(piece, "H", at)
                        stack.append((pieces - {piece}) | {left, right})
                for at in range(c0, c1):
                    if self._can_split(piece, "V", at, edges):
                        left, right = split_rect(piece, "V", at)
                        stack.append((pieces - {piece}) | {left, right})
        return False

    def _service_ok(self, rows, cols, punched, targets):
        svc = open_service(":memory:")
        tickets = [
            {"ticket_id": f"T{i}", "side": "front", "rect": list(t), "coupon_id": f"T{i}", "D": 1, "requires": {"X": 1}}
            for i, t in enumerate(targets)
        ]
        svc.register_sheet("s", rows=rows, cols=cols, perforations=[list(e) for e in punched], tickets=tickets)
        try:
            svc.try_tear("s", [f"T{i}" for i in range(len(targets))])
            return True
        except BasketError:
            return False

    def test_random_single_and_double_target_reachability(self):
        rng = random.Random(424242)
        for trial in range(120):
            rows, cols = rng.randint(1, 3), rng.randint(1, 3)
            all_edges = [("V", r, c) for r in range(rows) for c in range(cols - 1)]
            all_edges += [("H", r, c) for r in range(rows - 1) for c in range(cols)]
            punched = [e for e in all_edges if rng.random() < 0.6]
            edges = frozenset(punched)
            cells = [(r, c) for r in range(rows) for c in range(cols)]
            rng.shuffle(cells)
            single = (cells[0][0], cells[0][1], cells[0][0], cells[0][1])
            targets = [single]
            if len(cells) >= 2:
                second = (cells[1][0], cells[1][1], cells[1][0], cells[1][1])
                targets.append(second)
            with self.subTest(trial=trial, rows=rows, cols=cols, punched=sorted(punched), targets=targets):
                self.assertEqual(
                    self._reachable(rows, cols, edges, targets),
                    self._service_ok(rows, cols, punched, targets),
                )


class DifferentialTests(unittest.TestCase):
    """Cross-check the group optimiser against independent brute enumeration."""

    def test_random_groups_match_brute_force(self):
        rng = random.Random(20261122)
        for trial in range(80):
            baskets = []
            for bi in range(rng.randint(1, 3)):
                units = [
                    {"unit_id": f"b{bi}u{k}", "sku": rng.choice("XY"), "price": rng.randint(3, 12)}
                    for k in range(rng.randint(1, 2))
                ]
                baskets.append({"basket_id": f"b{bi}", "units": units})
            coupons = []
            copies = {}
            for ci in range(rng.randint(1, 2)):
                cid = f"c{ci}"
                copies[cid] = rng.randint(1, 2)
                coupons.append({"coupon_id": cid, "D": rng.randint(1, 5), "requires": {rng.choice("XY"): 1}})
            tickets = []
            index = 0
            for coupon in coupons:
                for _ in range(copies[coupon["coupon_id"]]):
                    tickets.append(
                        {
                            "ticket_id": f"t{index}",
                            "side": "front",
                            "rect": [0, index, 0, index],
                            "coupon_id": coupon["coupon_id"],
                            "D": coupon["D"],
                            "requires": coupon["requires"],
                        }
                    )
                    index += 1
            svc = open_service(":memory:")
            svc.register_sheet("s", rows=1, cols=index, perforations=[f"V0,{i}" for i in range(index - 1)], tickets=tickets)
            all_skus = {u["sku"] for b in baskets for u in b["units"]}
            accepted = [c for c in coupons if set(c["requires"]) <= all_skus]
            if not accepted:
                continue
            got = svc.quote_group("g", baskets, accepted_coupons=[c["coupon_id"] for c in accepted], store=False)["total_paid"]
            expected = quasi_brute_group(baskets, accepted, copies)
            with self.subTest(trial=trial):
                self.assertEqual(got, expected)


if __name__ == "__main__":
    unittest.main()
