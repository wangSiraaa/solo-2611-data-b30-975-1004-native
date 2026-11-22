"""Property-style checks: random small tables vs brute force, and scale runs."""

import random
import time
import unittest

from test_wire_codebook import brute_force_optimal
from wire.codebook import (
    DecodeError,
    UnloadableError,
    build_code_plan,
    decode,
    encode,
)


class RandomOptimalTests(unittest.TestCase):
    def test_random_small_tables_against_brute_force(self):
        rng = random.Random(20261122)
        for n in range(1, 6):
            for width in range(1, 5):
                for _ in range(40):
                    signals = tuple(f"s{i}" for i in range(n))
                    freqs = tuple(rng.randrange(0, 9) for _ in range(n))
                    burned = {}
                    if n >= 2 and rng.random() < 0.4:
                        leaf = rng.randrange(n)
                        length = rng.randrange(1, width + 1)
                        burned[signals[leaf]] = "".join(
                            rng.choice("01") for _ in range(length)
                        )
                    expected = brute_force_optimal(signals, freqs, width, burned)
                    if expected is None:
                        with self.assertRaises(UnloadableError):
                            build_code_plan(signals, freqs, width, burned=burned)
                        continue
                    plan = build_code_plan(signals, freqs, width, burned=burned)
                    self.assertEqual(
                        plan.total_bits,
                        sum(f * len(w) for f, w in zip(freqs, expected)),
                        (signals, freqs, width, burned),
                    )
                    for signal, code in burned.items():
                        self.assertEqual(plan.code_for[signal], code)

    def test_random_roundtrips(self):
        rng = random.Random(7)
        for _ in range(200):
            n = rng.randrange(1, 9)
            width = rng.randrange(1, 6)
            signals = tuple(f"sig{i}" for i in range(n))
            freqs = tuple(rng.randrange(0, 20) for _ in range(n))
            try:
                plan = build_code_plan(signals, freqs, width)
            except UnloadableError:
                continue
            sequence = [signals[rng.randrange(n)] for _ in range(rng.randrange(1, 50))]
            stream = encode(plan, sequence)
            self.assertEqual(decode(plan, stream), sequence)


class ScaleTests(unittest.TestCase):
    def test_four_to_thirty_two_signals_width_twelve(self):
        rng = random.Random(99)
        timings = []
        for n in (4, 8, 16, 24, 32):
            signals = tuple(f"node{i:02d}" for i in range(n))
            freqs = tuple(rng.randrange(0, 50) for _ in range(n))
            start = time.perf_counter()
            plan = build_code_plan(signals, freqs, max_length=12)
            elapsed = time.perf_counter() - start
            timings.append((n, elapsed))
            self.assertEqual(len(plan.codewords), n)
            self.assertEqual(len(set(plan.codewords)), n)
            self.assertTrue(all(len(w) <= 12 for w in plan.codewords))
            self.assertEqual(
                plan.total_bits,
                sum(f * len(w) for f, w in zip(freqs, plan.codewords)),
            )
            # Lexicographic 0-before-1 order must equal table order.
            self.assertEqual(
                list(plan.codewords), sorted(plan.codewords)
            )
            sequence = [signals[rng.randrange(n)] for _ in range(500)]
            self.assertEqual(decode(plan, encode(plan, sequence)), sequence)
        # The stated scale must stay comfortably interactive (no hard limit in
        # the spec, but guard against accidental exponential blowups).
        self.assertLess(timings[-1][1], 2.0)

    def test_burned_machine_at_scale(self):
        rng = random.Random(123)
        n = 16
        signals = tuple(f"n{i:02d}" for i in range(n))
        freqs = tuple(rng.randrange(1, 40) for _ in range(n))
        burned = {signals[0]: "0000", signals[5]: "0101", signals[-1]: "1111"}
        plan = build_code_plan(signals, freqs, max_length=12, burned=burned)
        for signal, code in burned.items():
            self.assertEqual(plan.code_for[signal], code)
        sequence = [signals[rng.randrange(n)] for _ in range(300)]
        self.assertEqual(decode(plan, encode(plan, sequence)), sequence)

    def test_too_narrow_width_at_full_capacity(self):
        signals = tuple(f"s{i}" for i in range(4))
        with self.assertRaises(UnloadableError):
            build_code_plan(signals, (1, 1, 1, 1), max_length=1)

    def test_burned_prefix_pair_unloadable(self):
        with self.assertRaises(UnloadableError):
            build_code_plan(
                ("a", "b", "c"),
                (1, 1, 1),
                max_length=4,
                burned={"a": "0", "b": "01"},
            )

    def test_all_zero_frequencies_still_get_distinct_words(self):
        signals = tuple(f"s{i}" for i in range(6))
        plan = build_code_plan(signals, (0,) * 6, max_length=12)
        self.assertEqual(len(set(plan.codewords)), 6)
        self.assertEqual(plan.total_bits, 0)
        # Zero total bits means an empty stream decodes to an empty batch; the
        # words themselves remain valid decoders even though no ticket uses them.
        self.assertEqual(decode(plan, encode(plan, [])), [])
        self.assertEqual(decode(plan, plan.code_for["s0"]), ["s0"])
        # A stream that ends on an internal-node prefix (no word ends there)
        # is a dangling, undecodable tail.
        with self.assertRaises(DecodeError):
            decode(plan, "1")  # internal prefix: words here start 10/110/...


if __name__ == "__main__":
    unittest.main()
