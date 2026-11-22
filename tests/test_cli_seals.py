"""CLI wiring for seals / co-signing: parse SEAT=TYPE and run subcommands."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

from reagent.cli import main


def _run(argv: list[str]) -> tuple[int, dict]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(argv)
    text = buf.getvalue().strip()
    return code, (json.loads(text) if text else {})


class SealCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "cli.db")
        self.inv = os.path.join(self._tmp.name, "inv.json")
        with open(self.inv, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "initial_day": 1,
                    "kinds": [{"kind": "R", "open_lifetime_days": 100}],
                    "bottles": [
                        {
                            "bottle_id": "X",
                            "kind": "R",
                            "total_portions": 2,
                            "intake_day": 1,
                            "printed_expiry_day": 500,
                            "opened_day": None,
                        },
                        {
                            "bottle_id": "Y",
                            "kind": "R",
                            "total_portions": 1,
                            "intake_day": 1,
                            "printed_expiry_day": 500,
                            "opened_day": None,
                        },
                    ],
                    "methods": [
                        {"method_id": "M", "revision": 1, "requirements": {"R": 1}},
                        {"method_id": "M2", "revision": 1, "requirements": {"R": 2}},
                    ],
                },
                handle,
            )
        self.wit = os.path.join(self._tmp.name, "wit.json")
        with open(self.wit, "w", encoding="utf-8") as handle:
            json.dump({"r1": ["A", "B"], "r2": ["C"]}, handle)
        self.spec = os.path.join(self._tmp.name, "spec.json")
        with open(self.spec, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "groups": [
                        {"name": "AB", "seats": ["A", "B"], "fee": 1},
                        {"name": "AC", "seats": ["A", "C"], "fee": 1},
                        {"name": "BC", "seats": ["B", "C"], "fee": 1},
                    ],
                    "shifts": [
                        {"shift_id": "s2", "on_duty": ["A", "C"]},
                        {"shift_id": "s3", "on_duty": ["B", "C"]},
                    ],
                },
                handle,
            )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _base(self) -> None:
        self.assertEqual(_run(["--db", self.db, "init", "--inventory", self.inv])[0], 0)
        _run([
            "--db", self.db, "create", "--run-id", "r1", "--method-id", "M2",
            "--method-revision", "1", "--day", "1", "--bottle", "X=1",
            "--bottle", "Y=1",
        ])
        _run([
            "--db", self.db, "create", "--run-id", "r2", "--method-id", "M",
            "--method-revision", "1", "--day", "1", "--bottle", "X=1",
        ])

    def test_seal_scheme_verify_post_flow(self) -> None:
        self._base()
        code, _ = _run([
            "--db", self.db, "seats", "--seat", "A=ordinary",
            "--seat", "B=ordinary", "--seat", "C=ordinary", "--day", "1",
        ])
        self.assertEqual(code, 0)
        code, seal = _run([
            "--db", self.db, "seal", "--seal-id", "S1", "--run", "r1",
            "--witness", self.wit, "--day", "1",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(seal["closure_runs"], ["r1", "r2"])

        code, scheme = _run([
            "--db", self.db, "scheme", "--scheme-id", "SCH1", "--seal-id", "S1",
            "--spec", self.spec, "--day", "1",
        ])
        self.assertEqual(code, 0)
        self.assertTrue(scheme["feasible"])
        self.assertEqual(scheme["total_fee"], 2)

        code, verify = _run([
            "--db", self.db, "verify-voucher", "--seal-id", "S1",
            "--scheme-id", "SCH1", "--shift", "s2", "--settlement-no", "T1",
            "--signature", "A", "--signature", "C",
        ])
        self.assertEqual(code, 0)
        self.assertTrue(verify["accepted"])

        code, posted = _run([
            "--db", self.db, "post", "--seal-id", "S1", "--scheme-id", "SCH1",
            "--shift", "s2", "--settlement-no", "T1", "--signature", "A",
            "--signature", "C", "--day", "1",
        ])
        self.assertEqual(code, 0)
        self.assertTrue(posted["posted"])
        self.assertFalse(posted["replayed"])

        code, replay = _run([
            "--db", self.db, "post", "--seal-id", "S1", "--scheme-id", "SCH1",
            "--shift", "s2", "--settlement-no", "T1", "--signature", "A",
            "--signature", "C", "--day", "2",
        ])
        self.assertEqual(code, 0)
        self.assertTrue(replay["replayed"])

    def test_historical_recall_flow(self) -> None:
        self._base()
        _run([
            "--db", self.db, "seats", "--seat", "A=ordinary",
            "--seat", "B=ordinary", "--seat", "C=ordinary", "--day", "1",
        ])
        _run([
            "--db", self.db, "seal", "--seal-id", "S1", "--run", "r1",
            "--witness", self.wit, "--day", "1",
        ])
        _run([
            "--db", self.db, "historical", "--group-id", "oldA", "--seat", "A",
            "--day", "2",
        ])
        _, blocked = _run([
            "--db", self.db, "scheme", "--scheme-id", "SCH1", "--seal-id", "S1",
            "--spec", self.spec, "--day", "2",
        ])
        self.assertFalse(blocked["feasible"])

        _run([
            "--db", self.db, "historical", "--group-id", "oldA", "--recall",
            "--day", "3",
        ])
        _, allowed = _run([
            "--db", self.db, "scheme", "--scheme-id", "SCH2", "--seal-id", "S1",
            "--spec", self.spec, "--day", "3",
        ])
        self.assertTrue(allowed["feasible"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
