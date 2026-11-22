"""转换通道与逐跳试换的单元测试。"""
import unittest

from sql_param.channels import Channel, ChannelCatalog, typed_equal
from sql_param.errors import ChannelError
from sql_param.plan_store import PlanStore

import tempfile
from pathlib import Path


def build_store():
    tmp = tempfile.TemporaryDirectory()
    store = PlanStore(Path(tmp.name) / "store")
    store.publish("ord", 1, "INSERT INTO t(id, a, b) VALUES(:id, :a, :b)")
    store.publish("ord", 2,
                  "INSERT INTO t(id, k) VALUES(:id, :k) /* :fake */ -- :fake2")
    store.publish("ord", 3, "INSERT INTO t(id, x, y, p) VALUES(:id, :x, :y, :x)")
    store.publish("ord", 4, "INSERT INTO t(id, q) VALUES(:id, :q)")
    return tmp, store


def catalog(store, channels):
    return ChannelCatalog(store, "ord", [Channel.from_dict(c) for c in channels])


STD = [
    {"channel_id": "c12", "source_revision": 1, "target_revision": 2, "fee": 1,
     "mapping": {"id": ["id"], "k": ["a", "b"]}},
    {"channel_id": "c23", "source_revision": 2, "target_revision": 3, "fee": 1,
     "mapping": {"id": ["id"], "x": ["k"], "y": ["k"]}},
    {"channel_id": "c13", "source_revision": 1, "target_revision": 3, "fee": 4,
     "mapping": {"id": ["id"], "x": ["a"], "y": ["b"]}},
]


class TypedEqualTests(unittest.TestCase):
    def test_four_value_families_never_mix(self):
        self.assertTrue(typed_equal(1, 1))
        self.assertTrue(typed_equal("s", "s"))
        self.assertTrue(typed_equal(b"\x01", b"\x01"))
        self.assertTrue(typed_equal(None, None))
        self.assertFalse(typed_equal("AB", b"AB"))
        self.assertFalse(typed_equal(1, "1"))
        self.assertFalse(typed_equal(None, 0))
        self.assertFalse(typed_equal(b"", ""))


class ChannelValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.store = build_store()
        self.addCleanup(self.tmp.cleanup)

    def test_mapping_checked_against_actual_plans(self):
        # 目标参数必须全覆盖、不能多。
        with self.assertRaises(ChannelError):
            catalog(self.store, [{**STD[0], "mapping": {"k": ["a", "b"]}}])
        # 来源名必须真实存在：注释里的 :fake 不算。
        with self.assertRaises(ChannelError):
            catalog(self.store, [
                {"channel_id": "x", "source_revision": 2, "target_revision": 3,
                 "fee": 1, "mapping": {"id": ["id"], "x": ["fake"], "y": ["k"]}}])
        # 所有来源名必须被使用（r1 的 b 未被引用）。
        with self.assertRaises(ChannelError):
            catalog(self.store, [
                {"channel_id": "x", "source_revision": 1, "target_revision": 3,
                 "fee": 1, "mapping": {"id": ["id"], "x": ["a"], "y": ["a"]}}])
        # 只通往更高修订。
        with self.assertRaises(ChannelError):
            catalog(self.store, [
                {"channel_id": "x", "source_revision": 3, "target_revision": 2,
                 "fee": 1, "mapping": {"id": ["id"], "x": ["x"], "y": ["y"]}}])
        # 同一目标内重复来源名拒绝。
        with self.assertRaises(ChannelError):
            Channel.from_dict(
                {"channel_id": "x", "source_revision": 1, "target_revision": 2,
                 "fee": 1, "mapping": {"id": ["id"], "k": ["a", "a"]}})

    def test_limits_16_channels_12_revisions(self):
        # 17 条通道拒绝。
        many = [
            {"channel_id": f"c{i}", "source_revision": 1, "target_revision": 2,
             "fee": 1, "mapping": {"id": ["id"], "k": ["a", "b"]}}
            for i in range(17)]
        with self.assertRaises(ChannelError):
            catalog(self.store, many)
        # 涉及 13 个修订拒绝（先补发布到 13 个修订，计划存储本身允许）。
        for rev in range(5, 14):
            self.store.publish("ord", rev, f"INSERT INTO t VALUES(:id, :p{rev})")
        wide = [
            {"channel_id": f"w{rev}", "source_revision": rev, "target_revision": rev + 1,
             "fee": 1,
             "mapping": ({"id": ["id"], f"p{rev + 1}": [f"p{rev}"]} if rev >= 4
                         else {"id": ["id"], "q": ["q"]})}
            for rev in range(1, 13)]
        with self.assertRaises(ChannelError):
            catalog(self.store, wide)

    def test_duplicate_channel_id_rejected(self):
        with self.assertRaises(ChannelError):
            catalog(self.store, [STD[0], {**STD[1], "channel_id": "c12"}])


class TryHopTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.store = build_store()
        self.addCleanup(self.tmp.cleanup)
        self.catalog = catalog(self.store, STD)

    def test_multi_source_merge_requires_same_type_and_value(self):
        ok = self.catalog.try_hop(self.catalog.by_id["c12"],
                                  {"id": "i1", "a": 3, "b": 3})
        self.assertTrue(ok.ok)
        self.assertEqual(ok.out_bindings, {"id": "i1", "k": 3})
        self.assertEqual(ok.resolved, {"id": "i1", "k": 3})

        bad = self.catalog.try_hop(self.catalog.by_id["c12"],
                                   {"id": "i2", "a": 3, "b": 4})
        self.assertFalse(bad.ok)
        self.assertIn("disagree", bad.error)
        self.assertIsNone(bad.out_bindings)

    def test_str_and_same_content_bytes_disagree(self):
        hop = self.catalog.try_hop(self.catalog.by_id["c12"],
                                   {"id": "i", "a": "AB", "b": {"$bytes": "4142"}})
        self.assertFalse(hop.ok)
        self.assertIn("str", hop.error)
        self.assertIn("bytes", hop.error)

    def test_null_merges_only_with_null(self):
        self.assertTrue(self.catalog.try_hop(
            self.catalog.by_id["c12"], {"id": "i", "a": None, "b": None}).ok)
        hop = self.catalog.try_hop(
            self.catalog.by_id["c12"], {"id": "i", "a": None, "b": 0})
        self.assertFalse(hop.ok)

    def test_binding_keys_must_match_source_plan(self):
        hop = self.catalog.try_hop(self.catalog.by_id["c12"], {"a": 1, "b": 1})
        self.assertFalse(hop.ok)
        self.assertIn("binding keys", hop.error)

    def test_boolean_rejected_at_hop(self):
        hop = self.catalog.try_hop(self.catalog.by_id["c12"],
                                   {"id": "i", "a": True, "b": True})
        self.assertFalse(hop.ok)
        self.assertIn("boolean", hop.error)

    def test_one_source_feeds_many_targets(self):
        hop = self.catalog.try_hop(self.catalog.by_id["c23"],
                                   {"id": "i", "k": 7})
        self.assertTrue(hop.ok)
        self.assertEqual(hop.out_bindings, {"id": "i", "x": 7, "y": 7})


class PathEnumerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.store = build_store()
        self.addCleanup(self.tmp.cleanup)
        self.catalog = catalog(self.store, STD)
        self.bindings = {"id": "i", "a": 3, "b": 3}

    def test_all_success_paths_sorted_by_hops_then_id(self):
        paths = self.catalog.enumerate_paths(1, self.bindings, 3)
        self.assertEqual([p["hops"] for p in paths], [["c13"], ["c12", "c23"]])
        # 每一步的值与最终绑定都能取回，且不依赖数据库。
        two_hop = paths[1]
        self.assertEqual([h["channel_id"] for h in two_hop["hop_results"]],
                         ["c12", "c23"])
        self.assertEqual(two_hop["final_bindings"],
                         {"id": "i", "x": 3, "y": 3})

    def test_checks_filter_paths_to_declared_target_values(self):
        ok = self.catalog.enumerate_paths(1, self.bindings, 3, {"x": 3})
        self.assertEqual(len(ok), 2)
        none = self.catalog.enumerate_paths(1, self.bindings, 3, {"x": 9})
        self.assertEqual(none, [])

    def test_value_specific_feasibility_same_channel_differs_per_order(self):
        # (3,4)：合并通道失败，只剩直达。
        paths = self.catalog.enumerate_paths(
            1, {"id": "i", "a": 3, "b": 4}, 3, {"x": 3})
        self.assertEqual([p["hops"] for p in paths], [["c13"]])

    def test_zero_hop_path_when_already_at_target(self):
        paths = self.catalog.enumerate_paths(
            3, {"id": "i", "x": 1, "y": 2}, 3, {"x": 1})
        self.assertEqual(paths[0]["hops"], [])
        rejected = self.catalog.enumerate_paths(
            3, {"id": "i", "x": 1, "y": 2}, 3, {"x": 2})
        self.assertEqual(rejected, [])

    def test_bytes_round_trip_through_path(self):
        # 同内容字节在多源合并与直达上都成立；str/bytes 分歧已在 TryHop 用例覆盖。
        paths = self.catalog.enumerate_paths(
            1, {"id": "i", "a": {"$bytes": "00ff"}, "b": {"$bytes": "00ff"}},
            3, {"x": {"$bytes": "00ff"}})
        self.assertEqual([p["hops"] for p in paths], [["c13"], ["c12", "c23"]])
        self.assertEqual(paths[0]["final_bindings"]["x"], {"$bytes": "00ff"})

    def test_str_bytes_divergence_leaves_only_direct_path(self):
        paths = self.catalog.enumerate_paths(
            1, {"id": "i", "a": "AB", "b": {"$bytes": "4142"}}, 3)
        self.assertEqual([p["hops"] for p in paths], [["c13"]])


if __name__ == "__main__":
    unittest.main()
