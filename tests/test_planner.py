"""周度选道/选单规划与重送台账的单元测试。"""
import json
import tempfile
import unittest
from pathlib import Path

from sql_param.channels import Channel, ChannelCatalog
from sql_param.errors import PlanningError
from sql_param.plan_store import PlanStore
from sql_param.planner import WeeklyLedger, WeeklyPlanner


def make_catalog():
    tmp = tempfile.TemporaryDirectory()
    store = PlanStore(Path(tmp.name) / "store")
    store.publish("ord", 1, "INSERT INTO t VALUES(:id, :a, :b)")
    store.publish("ord", 2, "INSERT INTO t VALUES(:id, :k)")
    store.publish("ord", 3, "INSERT INTO t VALUES(:id, :x, :y, :x)")
    channels = [
        {"channel_id": "c12", "source_revision": 1, "target_revision": 2, "fee": 1,
         "mapping": {"id": ["id"], "k": ["a", "b"]}},
        {"channel_id": "c23", "source_revision": 2, "target_revision": 3, "fee": 1,
         "mapping": {"id": ["id"], "x": ["k"], "y": ["k"]}},
        {"channel_id": "c13", "source_revision": 1, "target_revision": 3, "fee": 4,
         "mapping": {"id": ["id"], "x": ["a"], "y": ["b"]}},
    ]
    catalog = ChannelCatalog(store, "ord", [Channel.from_dict(c) for c in channels])
    return tmp, catalog


def order(order_id, a, b, check_x, priority=2):
    return {"order_id": order_id, "source_revision": 1, "target_revision": 3,
            "priority": priority,
            "bindings": {"id": f"id-{order_id}", "a": a, "b": b},
            "checks": {"x": check_x}}


class SampleScenarioTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.catalog = make_catalog()
        self.addCleanup(self.tmp.cleanup)
        self.planner = WeeklyPlanner(self.catalog)

    def test_budget_two_buys_two_cheap_channels_and_takes_both(self):
        plan = self.planner.plan_week("W1", 2, [order("S-1", 3, 3, 3),
                                                order("S-2", 3, 3, 3)])
        self.assertEqual(plan["purchased_channel_ids"], ["c12", "c23"])
        self.assertEqual(plan["channel_cost"], 2)
        self.assertEqual(plan["total_score"], 4)
        self.assertEqual(plan["considered_subsets"], 8)
        self.assertTrue(all(o["status"] == "accepted" for o in plan["orders"]))
        # 回执可还原通道与每跳修订。
        route = plan["orders"][0]["route"]
        self.assertEqual(route["hops"], ["c12", "c23"])
        self.assertEqual([(h["from_revision"], h["to_revision"]) for h in
                          route["hop_results"]], [(1, 2), (2, 3)])

    def test_changed_second_order_budget_two_takes_only_first(self):
        plan = self.planner.plan_week("W2", 2, [order("T-1", 3, 3, 3),
                                                order("T-2", 3, 4, 3)])
        accepted = [o["order_id"] for o in plan["orders"] if o["status"] == "accepted"]
        self.assertEqual(accepted, ["T-1"])
        rejected = plan["orders"][1]
        self.assertEqual(rejected["status"], "rejected")
        self.assertIn("purchased", rejected["reason"])

    def test_budget_four_prefers_direct_channel_for_both_orders(self):
        plan = self.planner.plan_week("W3", 4, [order("T-1", 3, 3, 3),
                                                order("T-2", 3, 4, 3)])
        # 两条都能落库：总分 4；费用 4 的单通道胜过费用 6 的三通道。
        self.assertEqual(plan["purchased_channel_ids"], ["c13"])
        self.assertEqual(plan["channel_cost"], 4)
        self.assertEqual(plan["total_score"], 4)
        self.assertTrue(all(o["status"] == "accepted" for o in plan["orders"]))

    def test_declared_check_fields_govern_delivery(self):
        # 同样 (3,3) 的单，声明 x=9：没有任何路径满足，有钱也不接。
        plan = self.planner.plan_week("W4", 99, [order("M1", 3, 3, 9)])
        self.assertEqual(plan["total_score"], 0)
        self.assertEqual(plan["purchased_channel_ids"], [])
        self.assertEqual(plan["orders"][0]["status"], "rejected")
        self.assertIn("declared target check", plan["orders"][0]["reason"])


class OptimizationRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.catalog = make_catalog()
        self.addCleanup(self.tmp.cleanup)
        self.planner = WeeklyPlanner(self.catalog)

    def test_max_score_then_min_cost_then_smallest_mask(self):
        # 高价单只能走 c13（a!=b 使合并通道不可用）；两个低价同形单走两跳通道即可。
        # 预算 4：买 c13 能同时覆盖三单（7 分，费 4），买 c12+c23 只覆盖两个低价单
        # （2 分，费 2）——总分优先，必须选 c13。
        cheap1 = order("C1", 3, 3, 3, priority=1)
        cheap2 = order("C2", 3, 3, 3, priority=1)
        pricey = order("P1", 3, 4, 3, priority=5)
        plan = self.planner.plan_week("W", 4, [cheap1, cheap2, pricey])
        self.assertEqual(plan["total_score"], 7)
        self.assertEqual(plan["purchased_channel_ids"], ["c13"])
        self.assertEqual(plan["channel_cost"], 4)

        # 同分费用最低：只送两个低价同形单、预算 4 时，费 2 的两跳组合胜过费 4 直达。
        plan2 = self.planner.plan_week("W2", 4, [cheap1, cheap2])
        self.assertEqual(plan2["total_score"], 2)
        self.assertEqual(plan2["purchased_channel_ids"], ["c12", "c23"])
        self.assertEqual(plan2["channel_cost"], 2)

    def test_zero_budget_rejects_everything(self):
        plan = self.planner.plan_week("W", 0, [order("S", 3, 3, 3)])
        self.assertEqual(plan["total_score"], 0)
        self.assertEqual(plan["channel_cost"], 0)
        self.assertTrue(all(o["status"] == "rejected" for o in plan["orders"]))

    def test_fee_paid_once_even_when_thousands_share_channels(self):
        orders = [order(f"U-{i:04d}", 3, 3, 3, priority=1) for i in range(1000)]
        plan = self.planner.plan_week("W-uniform", 2, orders)
        self.assertEqual(plan["channel_cost"], 2)  # 不是 2 * 1000
        self.assertEqual(plan["total_score"], 1000)
        self.assertEqual(sum(o["status"] == "accepted" for o in plan["orders"]), 1000)

    def test_mixed_store_each_order_evaluated_on_own_values(self):
        orders = [order(f"X-{i}", i % 5, (i + 1) % 5, i % 5, priority=1)
                  for i in range(20)]
        # 末单随单核对一个不可能的目标值：图上有路但核对不过，逐单拒绝。
        orders.append(order("X-bad", 3, 4, 99, priority=1))
        plan = self.planner.plan_week("W-mixed", 9, orders)
        statuses = [o["status"] for o in plan["orders"]]
        self.assertIn("accepted", statuses)
        self.assertIn("rejected", statuses)
        self.assertEqual(statuses[-1], "rejected")
        # 被接的单都有完整通道路径；被拒的单不产生绑定。
        for result in plan["orders"]:
            if result["status"] == "accepted":
                self.assertTrue(result["route"]["hops"])
            else:
                self.assertNotIn("route", result)

    def test_exhaustive_scoring_matches_brute_force(self):
        orders = [
            order("A", 3, 3, 3, priority=4),
            order("B", 3, 4, 3, priority=3),
            order("C", 5, 5, 5, priority=2),
            order("D", 1, 2, 1, priority=7),
        ]
        budget = 5
        plan = self.planner.plan_week("W", budget, orders)
        channels = self.catalog.channels

        def brute_force(candidate_orders, budget):
            best = None
            for bits in range(1 << len(channels)):
                fee = sum(c.fee for i, c in enumerate(channels) if bits & (1 << i))
                if fee > budget:
                    continue
                score = 0
                for candidate in candidate_orders:
                    evaluation = self.planner.evaluate_order(candidate)
                    if any((mask & ~bits) == 0 for mask in evaluation.minimal_masks):
                        score += candidate["priority"]
                key = (-score, fee, bits)
                if best is None or key < best[0]:
                    best = (key, score, fee, bits)
            return best

        _, score, fee, bits = brute_force(orders, budget)
        self.assertEqual(plan["total_score"], score)
        self.assertEqual(plan["channel_cost"], fee)
        expected_ids = [c.channel_id for i, c in enumerate(channels) if bits & (1 << i)]
        self.assertEqual(plan["purchased_channel_ids"], expected_ids)

    def test_randomized_planner_matches_brute_force(self):
        import random
        rng = random.Random(20261122)
        channels = self.catalog.channels
        for trial in range(40):
            orders = []
            for i in range(rng.randint(1, 12)):
                a = rng.choice([None, 1, 2, 3, "AB", {"$bytes": "4142"}])
                b = rng.choice([None, 1, 2, 3, "AB", {"$bytes": "4142"}])
                check = rng.choice([None, 1, 2, 3, 9])
                candidate = {
                    "order_id": f"r{trial}-{i}", "source_revision": 1,
                    "target_revision": 3, "priority": rng.randint(0, 9),
                    "bindings": {"id": f"id-{trial}-{i}", "a": a, "b": b},
                    "checks": {} if check is None else {"x": check},
                }
                orders.append(candidate)
            budget = rng.randint(0, 8)
            plan = self.planner.plan_week(f"R{trial}", budget, orders)

            best_key = None
            for bits in range(1 << len(channels)):
                fee = sum(c.fee for j, c in enumerate(channels) if bits & (1 << j))
                if fee > budget:
                    continue
                score = 0
                for candidate in orders:
                    evaluation = self.planner.evaluate_order(candidate)
                    if any((mask & ~bits) == 0 for mask in evaluation.minimal_masks):
                        score += candidate["priority"]
                key = (-score, fee, bits)
                if best_key is None or key < best_key:
                    best_key = key
            self.assertEqual(plan["total_score"], -best_key[0])
            self.assertEqual(plan["channel_cost"], best_key[1])
            # 每张订单的 accepted/rejected 必须与选中子集实际可执行性一致。
            chosen_bits = 0
            for j, c in enumerate(channels):
                if c.channel_id in plan["purchased_channel_ids"]:
                    chosen_bits |= 1 << j
            for candidate, result in zip(orders, plan["orders"]):
                evaluation = self.planner.evaluate_order(candidate)
                feasible = any((mask & ~chosen_bits) == 0
                               for mask in evaluation.minimal_masks)
                self.assertEqual(result["status"] == "accepted", feasible)

    def test_invalid_inputs_rejected(self):
        with self.assertRaises(PlanningError):
            self.planner.plan_week("W", 2, [])  # 空订单
        with self.assertRaises(PlanningError):
            self.planner.plan_week("W", -1, [order("S", 3, 3, 3)])
        with self.assertRaises(PlanningError):
            bad = order("S", 3, 3, 3)
            bad["priority"] = -2
            self.planner.plan_week("W", 2, [bad])
        with self.assertRaises(PlanningError):
            bad = order("S", 3, 3, 3)
            bad["order_id"] = "S"
            self.planner.plan_week("W", 2, [bad, order("S", 1, 1, 1)])  # 重复编号
        with self.assertRaises(PlanningError):
            bad = order("S", 3, 3, 3)
            bad["bindings"]["a"] = True  # bool 不是合法绑定值
            self.planner.plan_week("W", 2, [bad])

    def test_executor_items_use_target_revision_and_final_bindings(self):
        plan = self.planner.plan_week("W", 2, [order("S", 3, 3, 3)])
        items = self.planner.build_executor_items(plan)
        self.assertEqual(items, [{
            "template_id": "ord", "revision": 3,
            "bindings": {"id": "id-S", "x": 3, "y": 3},
        }])


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.catalog = make_catalog()
        self.addCleanup(self.tmp.cleanup)
        self.planner = WeeklyPlanner(self.catalog)
        self.ledger_path = Path(self.tmp.name) / "ledger.json"
        self.ledger = WeeklyLedger(self.ledger_path)

    def test_resend_verifies_original_result_and_detects_change(self):
        original = order("S-1", 3, 3, 3)
        plan = self.planner.plan_week("W1", 2, [original])
        self.ledger.record_week(plan)

        same = self.ledger.verify_resend("W1", order("S-1", 3, 3, 3))
        self.assertTrue(same["same_payload"])
        self.assertTrue(same["known_order"])
        self.assertEqual(same["original_status"], "accepted")
        self.assertEqual(same["original_route"]["hops"], ["c12", "c23"])

        changed = self.ledger.verify_resend("W1", order("S-1", 3, 4, 3))
        self.assertFalse(changed["same_payload"])

        unknown_order = self.ledger.verify_resend("W1", order("ZZZ", 1, 1, 1))
        self.assertTrue(unknown_order["known_week"])
        self.assertFalse(unknown_order["known_order"])
        unknown_week = self.ledger.verify_resend("nope", original)
        self.assertFalse(unknown_week["known_week"])

    def test_ledger_persists_across_reopen_and_week_is_immutable(self):
        plan = self.planner.plan_week("W1", 2, [order("S", 3, 3, 3)])
        self.ledger.record_week(plan)
        reopened = WeeklyLedger(self.ledger_path)
        self.assertTrue(reopened.verify_resend("W1", order("S", 3, 3, 3))["same_payload"])
        # 台账文件是规整 JSON。
        json.loads(self.ledger_path.read_text(encoding="utf-8"))
        with self.assertRaises(PlanningError):
            reopened.record_week(plan)


if __name__ == "__main__":
    unittest.main()
