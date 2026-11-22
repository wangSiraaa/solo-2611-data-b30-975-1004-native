import unittest

from wire.codebook import (
    DecodeError,
    UnloadableError,
    UnknownSignal,
    build_code_plan,
    decode,
    decode_trace,
    encode,
)


def brute_force_tables(n, max_length, burned=None):
    """Enumerate every alphabetic prefix-free table of n words within width.

    Used only by tests as an independent oracle for the interval DP.  A table
    is an ordered full binary tree whose leaves are the signals in table
    order; a leaf reached through d edges gives a d-bit word built from the
    left/right choices on its path.
    """

    burned = dict(burned or {})
    burned_at = {code: leaf for leaf, code in burned.items()}

    def gen(prefix, leaves, depth):
        """Yield every ordered word tuple fitting exactly ``leaves`` leaves.

        The subtree rooted at ``prefix`` must contain all ``leaves`` signals.
        Cases:
          * a burned node is one fixed leaf -- valid only when leaves == 1;
          * a free node with one leaf may stop here or extend down either
            child (one-sided chains, e.g. ``00 / 010``);
          * a free node with several leaves must split between its children.
        Prefix-freedom and lexicographic order are structural; an explicit
        filter below guards every emitted table.
        """

        if leaves == 0:
            # An empty child subtree contributes one empty tuple, so a one-sided
            # internal split (all leaves under the other child) still yields.
            yield ()
            return
        if depth > max_length:
            return
        if prefix in burned_at:
            if leaves == 1:
                yield (prefix,)
            return
        if leaves == 1:
            if depth >= 1:
                yield (prefix,)
            if depth < max_length:
                yield from gen(prefix + "0", 1, depth + 1)
                yield from gen(prefix + "1", 1, depth + 1)
            return
        if depth >= max_length:
            return
        # k may be 0 or leaves: prefix-code tries permit an unused child
        # (one-sided internal chains), which yields tables such as 00 / 010.
        for k in range(0, leaves + 1):
            for left_words in gen(prefix + "0", k, depth + 1):
                for right_words in gen(prefix + "1", leaves - k, depth + 1):
                    yield left_words + right_words

    tables = set()
    for words in gen("", n, 0):
        if len(words) != n or any(not w or len(w) > max_length for w in words):
            continue
        if any(b.startswith(a) for a, b in zip(words, words[1:])):
            continue
        if list(words) != sorted(words):
            continue
        if all(idx not in burned or words[idx] == code for idx, code in burned.items()):
            tables.add(words)
    return sorted(tables)


def brute_force_optimal(signals, frequencies, max_length, burned=None):
    burned = dict(burned or {})
    index = {signal: idx for idx, signal in enumerate(signals)}
    leaf_burn = {index[signal]: code for signal, code in burned.items()}
    tables = brute_force_tables(len(signals), max_length, leaf_burn)
    viable = [
        words
        for words in tables
        if all(not later.startswith(earlier) for earlier, later in zip(words, words[1:]))
        and all(earlier < later for earlier, later in zip(words, words[1:]))
    ]
    if not viable:
        return None
    return min(
        viable,
        key=lambda words: sum(freq * len(word) for freq, word in zip(frequencies, words)),
    )


class SampleScenarioTests(unittest.TestCase):
    SIGNALS = ("A", "AB", "B")

    def test_scenario_one_fifteen_bits(self):
        plan = build_code_plan(self.SIGNALS, (5, 2, 3), max_length=2)
        self.assertEqual(plan.codewords, ("0", "10", "11"))
        self.assertEqual(plan.total_bits, 15)

    def test_scenario_two_fourteen_bits_short_code_on_b(self):
        plan = build_code_plan(self.SIGNALS, (2, 2, 6), max_length=2)
        self.assertEqual(plan.total_bits, 14)
        # The only one-bit word is 1 and it must belong to the final signal B.
        self.assertEqual(plan.codewords, ("00", "01", "1"))
        self.assertEqual(len(plan.code_for["B"]), 1)

    def test_scenario_three_burned_a_is_seventeen_bits(self):
        plan = build_code_plan(
            self.SIGNALS,
            (5, 2, 3),
            max_length=2,
            burned={"A": "00"},
            machine_id="burn-A-00",
        )
        self.assertEqual(plan.code_for["A"], "00")
        self.assertEqual(plan.total_bits, 17)
        self.assertEqual(plan.codewords, ("00", "01", "1"))

    def test_zero_frequency_keeps_its_own_codeword(self):
        plan = build_code_plan(("A", "AB", "B"), (10, 0, 1), max_length=2)
        self.assertEqual(len(plan.codewords), 3)
        self.assertEqual(len(set(plan.codewords)), 3)
        # A takes the one-bit word; zero-frequency AB still occupies its own
        # independent word and is excluded from the cost by its zero count.
        self.assertEqual(plan.codewords, ("0", "10", "11"))
        self.assertEqual(plan.total_bits, 12)
        self.assertEqual(decode(plan, encode(plan, ["AB", "A", "AB"])), ["AB", "A", "AB"])


class UnloadableTests(unittest.TestCase):
    SIGNALS = ("A", "AB", "B")

    def test_width_one_cannot_hold_three_signals(self):
        with self.assertRaises(UnloadableError):
            build_code_plan(self.SIGNALS, (5, 2, 3), max_length=1)

    def test_burned_word_too_long(self):
        with self.assertRaises(UnloadableError):
            build_code_plan(self.SIGNALS, (5, 2, 3), max_length=2, burned={"A": "000"})

    def test_burned_prefix_conflict(self):
        with self.assertRaises(UnloadableError):
            build_code_plan(
                self.SIGNALS, (5, 2, 3), max_length=3, burned={"A": "0", "AB": "01"}
            )

    def test_burned_order_conflict(self):
        with self.assertRaises(UnloadableError):
            build_code_plan(
                self.SIGNALS, (5, 2, 3), max_length=3, burned={"A": "1", "B": "0"}
            )

    def test_burns_force_width_overflow(self):
        # B=000 and A=1 leaves no legal slot for AB between them within width 2.
        with self.assertRaises(UnloadableError):
            build_code_plan(
                self.SIGNALS, (5, 2, 3), max_length=2, burned={"B": "000", "A": "1"}
            )

    def test_unknown_burned_signal(self):
        with self.assertRaises(UnloadableError):
            build_code_plan(self.SIGNALS, (1, 1, 1), max_length=2, burned={"X": "0"})


class CodecTests(unittest.TestCase):
    def setUp(self):
        self.plan = build_code_plan(("A", "AB", "B"), (5, 2, 3), max_length=2)

    def test_roundtrip_stream_has_no_separator(self):
        stream = encode(self.plan, ["A", "A", "B", "AB", "B"])
        self.assertEqual(stream, "0" + "0" + "11" + "10" + "11")
        self.assertEqual(decode(self.plan, stream), ["A", "A", "B", "AB", "B"])

    def test_out_of_table_signal_bounces(self):
        with self.assertRaises(UnknownSignal):
            encode(self.plan, ["A", "XYZ"])

    def test_garbled_stream_detected(self):
        # Table 0/10/11: a dangling prefix that is no complete word fails when
        # the stream ends mid-codeword, and an impossible continuation under a
        # leaf is rejected because the decoder resets to the root after a leaf.
        with self.assertRaises(DecodeError):
            decode(self.plan, "1")  # prefix of 10/11, stream ends mid-word
        # 0 is a leaf; the decoder resets to root, where only 0 or 1 start a
        # word. A non-binary symbol is therefore rejected at the next bit.
        with self.assertRaises(DecodeError):
            decode(self.plan, "02")

    def test_trace_emits_one_step_per_codeword(self):
        stream = encode(self.plan, ["AB", "B", "A"])
        self.assertEqual(
            decode_trace(self.plan, stream),
            [("10", "AB"), ("11", "B"), ("0", "A")],
        )


class EdgeCaseTests(unittest.TestCase):
    def test_single_signal_always_gets_one_bit_word(self):
        for width in (1, 2, 12):
            plan = build_code_plan(("only",), (7,), width)
            self.assertEqual(plan.codewords, ("0",))
            self.assertEqual(plan.total_bits, 7)

    def test_full_capacity_tree_at_minimal_width(self):
        plan = build_code_plan(("a", "b", "c", "d"), (1, 2, 3, 4), 2)
        self.assertEqual(set(plan.codewords), {"00", "01", "10", "11"})

    def test_rejected_configurations(self):
        for kwargs in (
            dict(max_length=1),
            dict(max_length=2, burned={"a": "000"}),
            dict(max_length=2, burned={"a": "01", "b": "01"}),
            dict(max_length=2, burned={"a": ""}),
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(UnloadableError):
                    build_code_plan(("a", "b", "c"), (1, 1, 1), **kwargs)
        with self.assertRaises(UnloadableError):
            build_code_plan(("a",), (-1,), 2)

    def test_non_binary_stream_rejected(self):
        plan = build_code_plan(("a", "b"), (1, 1), 2)
        with self.assertRaises(DecodeError):
            decode(plan, "012")


class OptimalAgainstBruteForceTests(unittest.TestCase):
    def test_free_tables_match_brute_force(self):
        cases = [
            (("A", "AB", "B"), (5, 2, 3), 2),
            (("A", "AB", "B"), (2, 2, 6), 2),
            (("s0", "s1", "s2", "s3"), (4, 0, 2, 7), 2),
            (("s0", "s1", "s2", "s3"), (1, 2, 3, 4), 3),
            (("a", "b", "c", "d", "e"), (3, 1, 4, 1, 5), 3),
        ]
        for signals, freqs, width in cases:
            with self.subTest(signals=signals, width=width):
                expected = brute_force_optimal(signals, freqs, width)
                plan = build_code_plan(signals, freqs, width)
                self.assertEqual(
                    plan.total_bits,
                    sum(f * len(w) for f, w in zip(freqs, expected)),
                )

    def test_burned_tables_match_brute_force(self):
        signals = ("a", "b", "c", "d")
        freqs = (3, 1, 4, 2)
        burns = [
            {"a": "00"},
            {"d": "11"},
            {"b": "010", "c": "10"},
            {"a": "000", "d": "111"},
        ]
        for burned in burns:
            with self.subTest(burned=burned):
                expected = brute_force_optimal(signals, freqs, 3, burned)
                if expected is None:
                    with self.assertRaises(UnloadableError):
                        build_code_plan(signals, freqs, 3, burned=burned)
                else:
                    plan = build_code_plan(signals, freqs, 3, burned=burned)
                    self.assertEqual(
                        plan.total_bits,
                        sum(f * len(w) for f, w in zip(freqs, expected)),
                    )
                    for signal, code in burned.items():
                        self.assertEqual(plan.code_for[signal], code)

    def test_truly_unloadable_burn_matches_brute_force(self):
        # b is pinned at 010 while a is pinned at 1: their lexicographic order
        # is reversed, so no alphabetic table can honor both.
        signals = ("a", "b", "c", "d")
        freqs = (3, 1, 4, 2)
        self.assertIsNone(
            brute_force_optimal(signals, freqs, 3, {"b": "010", "a": "1"})
        )
        with self.assertRaises(UnloadableError):
            build_code_plan(signals, freqs, 3, burned={"b": "010", "a": "1"})


if __name__ == "__main__":
    unittest.main()
