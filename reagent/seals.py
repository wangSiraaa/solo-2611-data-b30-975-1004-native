"""Whole-bottle seals (封单) and offline co-signing (会签) on top of the ledger.

A *seal* freezes the manifest that a group of already-reserved runs must hand
over together: the transitive closure of runs and bottles, every run's trusted
witness seats, and the exact reservation rows. Sealing never deducts inventory.

A *scheme* chooses which candidate groups to activate. It must satisfy duty
coverage and mutual recognition; :mod:`reagent.cosign` holds the pure rules.

A *posting* is the single formal settlement of a seal. It is deduplicated by
``(seal, settlement number)`` and refuses to deliver if any sealed run has
already started or its reservations no longer match the frozen manifest.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterable, Mapping, Optional, Sequence

from . import bottles, cosign
from .errors import Conflict, NotFoundError, ValidationError
from .inventory import InventoryService
from .rules import advance_day, require_text
from .schema import transaction


class SealService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.inventory = InventoryService(conn)

    # ------------------------------------------------------------------
    # seats + historical groups
    # ------------------------------------------------------------------
    def register_seats(self, seats: Sequence[Mapping], day: Optional[int] = None) -> dict:
        """Register named seats and their type (ordinary / deputy).

        A seat's type is fixed forever: ordinary seats are single-owner and are
        what makes the mutual-recognition rule bite.
        """
        with transaction(self.conn):
            if day is not None:
                advance_day(self.conn, day)
            result = self._merge_seats(seats)
            return result

    def _merge_seats(self, seats: Sequence[Mapping]) -> dict:
        if not isinstance(seats, (list, tuple)) or not seats:
            raise ValidationError("seats must be a non-empty list")
        ordinary: list[str] = []
        deputy: list[str] = []
        seen: set[str] = set()
        for item in seats:
            if not isinstance(item, Mapping):
                raise ValidationError(f"seat entry must be an object: {item!r}")
            seat_id = require_text(item.get("seat_id"), "seat_id")
            seat_type = require_text(item.get("seat_type"), "seat_type")
            if seat_type not in cosign.SEAT_TYPES:
                raise ValidationError(
                    f"seat {seat_id}: unknown type {seat_type!r}"
                )
            if seat_id in seen:
                raise ValidationError(f"duplicate seat {seat_id!r}")
            seen.add(seat_id)
            row = self.conn.execute(
                "SELECT seat_type FROM seats WHERE seat_id = ?", (seat_id,)
            ).fetchone()
            if row is not None:
                if row["seat_type"] != seat_type:
                    raise Conflict(
                        f"seat {seat_id} is already registered as "
                        f"{row['seat_type']}, not {seat_type}"
                    )
            else:
                self.conn.execute(
                    "INSERT INTO seats(seat_id, seat_type) VALUES(?, ?)",
                    (seat_id, seat_type),
                )
            (ordinary if seat_type == cosign.ORDINARY else deputy).append(seat_id)
        return {"ordinary": sorted(ordinary), "deputy": sorted(deputy)}

    def _ordinary_seats(self) -> frozenset[str]:
        rows = self.conn.execute(
            "SELECT seat_id FROM seats WHERE seat_type = 'ordinary'"
        ).fetchall()
        return frozenset(r["seat_id"] for r in rows)

    def _require_known_seats(self, seat_ids: Iterable[str], where: str) -> None:
        known = {
            r["seat_id"] for r in self.conn.execute("SELECT seat_id FROM seats")
        }
        missing = sorted(set(seat_ids) - known)
        if missing:
            raise ValidationError(
                f"{where} references unregistered seat(s): {', '.join(missing)}"
            )

    def register_historical_group(
        self, group_id: str, seats: Sequence[str], day: int
    ) -> dict:
        """Record an old group that may still be redeemable until recalled."""
        with transaction(self.conn):
            advance_day(self.conn, day)
            group_id = require_text(group_id, "group_id")
            seat_set = _seat_set(seats, f"historical group {group_id}")
            existing = self.conn.execute(
                "SELECT seats_json, recalled FROM historical_groups WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            if existing is not None:
                if json.loads(existing["seats_json"]) != sorted(seat_set):
                    raise Conflict(
                        f"historical group {group_id} already registered with "
                        "different seats"
                    )
                return self._historical_view(group_id)
            self.conn.execute(
                "INSERT INTO historical_groups(group_id, seats_json, created_day, "
                "recalled) VALUES(?, ?, ?, 0)",
                (group_id, _dumps(sorted(seat_set)), day),
            )
            return self._historical_view(group_id)

    def recall_historical_group(self, group_id: str, day: int) -> dict:
        """Confirm an old group is withdrawn; the recall can never be undone."""
        with transaction(self.conn):
            advance_day(self.conn, day)
            group_id = require_text(group_id, "group_id")
            row = self.conn.execute(
                "SELECT 1 FROM historical_groups WHERE group_id = ?", (group_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown historical group {group_id!r}")
            self.conn.execute(
                "UPDATE historical_groups SET recalled = 1 WHERE group_id = ?",
                (group_id,),
            )
            return self._historical_view(group_id)

    def _historical_view(self, group_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM historical_groups WHERE group_id = ?", (group_id,)
        ).fetchone()
        return {
            "group_id": row["group_id"],
            "seats": json.loads(row["seats_json"]),
            "recalled": bool(row["recalled"]),
        }

    def _effective_historical(self) -> list[cosign.HistoricalGroup]:
        rows = self.conn.execute(
            "SELECT * FROM historical_groups WHERE recalled = 0 "
            "ORDER BY group_id"
        ).fetchall()
        return [
            cosign.HistoricalGroup(
                row["group_id"], frozenset(json.loads(row["seats_json"]))
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # seal creation
    # ------------------------------------------------------------------
    def create_seal(
        self,
        seal_id: str,
        designated_runs: Sequence[str],
        witnesses: Mapping[str, Sequence[str]],
        day: int,
    ) -> dict:
        """Freeze a whole-bottle handover manifest. Does not move inventory."""
        with transaction(self.conn):
            advance_day(self.conn, day)
            seal_id = require_text(seal_id, "seal_id")
            if self.conn.execute(
                "SELECT 1 FROM seals WHERE seal_id = ?", (seal_id,)
            ).fetchone():
                raise Conflict(f"seal {seal_id!r} already exists")
            if not isinstance(designated_runs, (list, tuple)) or not designated_runs:
                raise ValidationError("designated_runs must be a non-empty list")
            designated: list[str] = []
            for rid in designated_runs:
                rid = require_text(rid, "run_id")
                if rid in designated:
                    raise ValidationError(f"duplicate designated run {rid!r}")
                designated.append(rid)

            manifest = self._build_manifest(designated, witnesses, day)
            self.conn.execute(
                "INSERT INTO seals(seal_id, created_day, designated_runs_json, "
                "runs_json, bottles_json, witnesses_json, reservations_json, "
                "report_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    seal_id,
                    day,
                    _dumps(designated),
                    _dumps(manifest["closure_runs"]),
                    _dumps(manifest["bottles"]),
                    _dumps(manifest["witnesses"]),
                    _dumps(manifest["reservation_snapshot"]),
                    _dumps(manifest["report"]),
                ),
            )
            return self.get_seal(seal_id)

    def _build_manifest(
        self,
        designated: Sequence[str],
        witnesses: Mapping[str, Sequence[str]],
        day: int,
    ) -> dict:
        active_by_holder = self._active_by_holder()
        # run -> {bottle: [reservation rows]}
        for rid in designated:
            self._require_run(rid)
            if not active_by_holder.get(rid):
                raise ValidationError(
                    f"run {rid!r} has no active reservations and cannot be sealed"
                )

        run_reason: dict[str, str] = {}
        bottle_reason: dict[str, str] = {}
        order: list[str] = []
        queue: list[str] = []
        for rid in designated:
            run_reason[rid] = "designated by the waybill"
            order.append(rid)
            queue.append(rid)
        seen_runs = set(designated)
        seen_bottles: set[str] = set()

        # bottle -> runs with active reservations there
        bottle_runs: dict[str, set[str]] = {}
        for holder, rows in active_by_holder.items():
            if not self._is_run(holder):
                continue
            for row in rows:
                bottle_runs.setdefault(row["bottle_id"], set()).add(holder)

        while queue:
            rid = queue.pop(0)
            for row in active_by_holder[rid]:
                bid = row["bottle_id"]
                if bid not in seen_bottles:
                    seen_bottles.add(bid)
                    bottle_reason[bid] = f"holds active portions of run {rid}"
                for other in sorted(bottle_runs.get(bid, ())):
                    if other not in seen_runs:
                        seen_runs.add(other)
                        run_reason[other] = (
                            f"shares bottle {bid} with run {rid}"
                        )
                        order.append(other)
                        queue.append(other)

        # witnesses must be supplied for every closure run
        witnesses_out: dict[str, list[str]] = {}
        for rid in order:
            if rid not in witnesses:
                raise ValidationError(
                    f"missing witness seats for run {rid!r}"
                )
            seats = _seat_list(witnesses[rid], f"witnesses for {rid}")
            witnesses_out[rid] = seats
        extra = sorted(set(witnesses) - set(order))
        if extra:
            raise ValidationError(
                "witnesses supplied for runs outside the closure: "
                + ", ".join(extra)
            )

        bottle_facts: dict[str, dict] = {}
        for bid in sorted(seen_bottles):
            bottle_facts[bid] = self._bottle_manifest(
                bid, day, seen_runs
            )
            bottle_facts[bid]["reason"] = bottle_reason[bid]

        runs_out: dict[str, dict] = {}
        snapshot: dict[str, list[dict]] = {}
        for rid in order:
            rows = active_by_holder.get(rid, [])
            snapshot[rid] = [
                {
                    "reservation_id": row["reservation_id"],
                    "bottle_id": row["bottle_id"],
                    "portions": int(row["portions"]),
                    "status": row["status"],
                }
                for row in sorted(rows, key=lambda r: r["reservation_id"])
            ]
            per_bottle: dict[str, int] = {}
            for row in rows:
                per_bottle[row["bottle_id"]] = (
                    per_bottle.get(row["bottle_id"], 0) + int(row["portions"])
                )
            run_row = self.conn.execute(
                "SELECT state FROM runs WHERE run_id = ?", (rid,)
            ).fetchone()
            runs_out[rid] = {
                "run_id": rid,
                "state": run_row["state"],
                "designated": rid in designated,
                "reason": run_reason[rid],
                "witness_seats": witnesses_out[rid],
                "portions_by_bottle": per_bottle,
            }

        issues = self._collect_issues(bottle_facts)
        report = {
            "seal_id": None,
            "created_day": day,
            "designated_runs": list(designated),
            "closure_runs": order,
            "bottles": bottle_facts,
            "runs": runs_out,
            "issues": issues,
            "clean": not issues["any"],
            "fingerprint": _fingerprint(bottle_facts, runs_out, snapshot),
        }
        return {
            "closure_runs": order,
            "bottles": bottle_facts,
            "witnesses": witnesses_out,
            "reservation_snapshot": snapshot,
            "report": report,
        }

    def _bottle_manifest(
        self, bottle_id: str, day: int, closure_runs: set[str]
    ) -> dict:
        bottle = bottles.fetch_bottle(self.conn, bottle_id)
        rows = self.conn.execute(
            "SELECT holder_id, portions, status, reservation_id FROM reservations "
            "WHERE bottle_id = ?",
            (bottle_id,),
        ).fetchall()
        active_by_holder: dict[str, int] = {}
        consumed = 0
        for row in rows:
            if row["status"] == "active":
                active_by_holder[row["holder_id"]] = (
                    active_by_holder.get(row["holder_id"], 0) + int(row["portions"])
                )
            elif row["status"] == "consumed":
                consumed += int(row["portions"])
        reserved = sum(active_by_holder.values())
        unallocated = bottle.total_portions - reserved - consumed
        ordinary_holders = sorted(
            holder for holder in active_by_holder if not self._is_run(holder)
        )
        runs_here = sorted(
            (holder, portions)
            for holder, portions in active_by_holder.items()
            if holder in closure_runs
        )
        # any run holder on this bottle is necessarily in the closure
        outside_runs = sorted(
            holder
            for holder in active_by_holder
            if self._is_run(holder) and holder not in closure_runs
        )
        valid = bottle.usable_on(day)
        return {
            "bottle_id": bottle_id,
            "total_portions": bottle.total_portions,
            "consumed": consumed,
            "reserved": reserved,
            "unallocated": unallocated,
            "valid_on_day": valid,
            "validity": bottle.validity(day),
            "effective_expiry_day": bottle.effective_expiry_day,
            "ordinary_holders": ordinary_holders,
            "runs": [
                {"run_id": rid, "portions": portions} for rid, portions in runs_here
            ],
            "outside_runs": outside_runs,
        }

    def _collect_issues(self, bottle_facts: Mapping[str, Mapping]) -> dict:
        ordinary = []
        unallocated = []
        invalid = []
        outside = []
        for bid, facts in bottle_facts.items():
            if facts["ordinary_holders"]:
                ordinary.append(
                    {"bottle_id": bid, "holders": facts["ordinary_holders"]}
                )
            if facts["unallocated"] != 0:
                unallocated.append(
                    {"bottle_id": bid, "unallocated": facts["unallocated"]}
                )
            if not facts["valid_on_day"]:
                invalid.append(
                    {
                        "bottle_id": bid,
                        "validity": facts["validity"],
                        "effective_expiry_day": facts["effective_expiry_day"],
                    }
                )
            if facts["outside_runs"]:
                outside.append(
                    {"bottle_id": bid, "runs": facts["outside_runs"]}
                )
        return {
            "ordinary_holders": ordinary,
            "unallocated": unallocated,
            "invalid_bottles": invalid,
            "outside_runs": outside,
            "any": bool(ordinary or unallocated or invalid or outside),
        }

    def get_seal(self, seal_id: str) -> dict:
        row = self._seal_row(seal_id)
        report = json.loads(row["report_json"])
        report["seal_id"] = row["seal_id"]
        return {
            "seal_id": row["seal_id"],
            "created_day": row["created_day"],
            "designated_runs": json.loads(row["designated_runs_json"]),
            "closure_runs": json.loads(row["runs_json"]),
            "bottles": json.loads(row["bottles_json"]),
            "witnesses": json.loads(row["witnesses_json"]),
            "reservation_snapshot": json.loads(row["reservations_json"]),
            "report": report,
            "clean": report["clean"],
        }

    def seal_staleness(self, seal_id: str) -> dict:
        """Compare the frozen manifest with the current ledger.

        Re-reserving the same amount after a release produces fresh reservation
        ids, so the snapshot differs and the seal must be re-made.
        """
        seal = self.get_seal(seal_id)
        active_by_holder = self._active_by_holder()
        current_snapshot: dict[str, list[dict]] = {}
        for rid in seal["closure_runs"]:
            rows = active_by_holder.get(rid, [])
            current_snapshot[rid] = [
                {
                    "reservation_id": row["reservation_id"],
                    "bottle_id": row["bottle_id"],
                    "portions": int(row["portions"]),
                    "status": row["status"],
                }
                for row in sorted(rows, key=lambda r: r["reservation_id"])
            ]
        changed_runs = [
            rid
            for rid in seal["closure_runs"]
            if _normalize(current_snapshot.get(rid, []))
            != _normalize(seal["reservation_snapshot"].get(rid, []))
        ]
        # current run states
        bad_runs = []
        for rid in seal["closure_runs"]:
            row = self.conn.execute(
                "SELECT state FROM runs WHERE run_id = ?", (rid,)
            ).fetchone()
            if row is None or row["state"] != "reserved":
                bad_runs.append(
                    {"run_id": rid, "state": None if row is None else row["state"]}
                )
        # re-derive bottle facts and compare structural fingerprint
        current_bottles = {}
        for bid in seal["bottles"]:
            current_bottles[bid] = self._bottle_manifest(
                bid, seal["created_day"], set(seal["closure_runs"])
            )
        current_runs_view = {
            rid: {
                "run_id": rid,
                "state": next(
                    (
                        r["state"]
                        for r in self.conn.execute(
                            "SELECT state FROM runs WHERE run_id = ?", (rid,)
                        ).fetchall()
                    ),
                    None,
                ),
                "designated": rid in seal["designated_runs"],
                "reason": seal["report"]["runs"][rid]["reason"],
                "witness_seats": seal["witnesses"][rid],
                "portions_by_bottle": _portions_by_bottle(
                    current_snapshot.get(rid, [])
                ),
            }
            for rid in seal["closure_runs"]
        }
        current_fp = _fingerprint(current_bottles, current_runs_view, current_snapshot)
        structural_changed = current_fp != seal["report"]["fingerprint"]
        stale = bool(changed_runs or bad_runs or structural_changed)
        return {
            "seal_id": seal_id,
            "stale": stale,
            "changed_runs": changed_runs,
            "non_reserved_runs": bad_runs,
            "structural_change": structural_changed,
            "requires_reseal": stale,
        }

    # ------------------------------------------------------------------
    # schemes (co-signing plans)
    # ------------------------------------------------------------------
    def create_scheme(
        self,
        scheme_id: str,
        seal_id: str,
        groups: Sequence[Mapping],
        shifts: Sequence[Mapping],
        seats: Sequence[Mapping],
        day: int,
    ) -> dict:
        """Activate the minimum-fee group set for a seal's handover windows."""
        with transaction(self.conn):
            advance_day(self.conn, day)
            scheme_id = require_text(scheme_id, "scheme_id")
            seal = self.get_seal(seal_id)
            if self.conn.execute(
                "SELECT 1 FROM schemes WHERE scheme_id = ?", (scheme_id,)
            ).fetchone():
                raise Conflict(f"scheme {scheme_id!r} already exists")

            self._merge_seats(seats) if seats else None
            parsed_groups = _parse_groups(groups)
            parsed_shifts = _parse_shifts(shifts)

            all_group_seats = set().union(*(g.seats for g in parsed_groups))
            self._require_known_seats(all_group_seats, "groups")
            for rid, wseats in seal["witnesses"].items():
                self._require_known_seats(wseats, f"witnesses of {rid}")
            for old in self._effective_historical():
                self._require_known_seats(old.seats, f"historical group {old.group_id}")

            ordinary = self._ordinary_seats()
            witnesses = {rid: set(s) for rid, s in seal["witnesses"].items()}
            plan = cosign.plan_scheme(
                parsed_groups,
                parsed_shifts,
                seal["closure_runs"],
                witnesses,
                ordinary,
                self._effective_historical(),
            )
            groups_payload = [g.as_dict() for g in parsed_groups]
            shifts_payload = [s.as_dict() for s in parsed_shifts]
            result = plan.as_dict()
            result["shifts"] = shifts_payload
            result["groups"] = {
                g.name: g.as_dict() for g in parsed_groups
            }
            self.conn.execute(
                "INSERT INTO schemes(scheme_id, seal_id, created_day, groups_json, "
                "result_json, feasible, total_fee) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    scheme_id,
                    seal["seal_id"],
                    day,
                    _dumps(groups_payload),
                    _dumps(result),
                    1 if plan.feasible else 0,
                    plan.total_fee,
                ),
            )
            return self.get_scheme(scheme_id)

    def get_scheme(self, scheme_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM schemes WHERE scheme_id = ?", (scheme_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown scheme {scheme_id!r}")
        result = json.loads(row["result_json"])
        return {
            "scheme_id": row["scheme_id"],
            "seal_id": row["seal_id"],
            "created_day": row["created_day"],
            "feasible": bool(row["feasible"]),
            "total_fee": row["total_fee"],
            "groups": json.loads(row["groups_json"]),
            "plan": result,
        }

    # ------------------------------------------------------------------
    # voucher verification + formal posting
    # ------------------------------------------------------------------
    def verify_voucher(
        self,
        seal_id: str,
        scheme_id: str,
        shift_id: str,
        settlement_no: str,
        signatures: Sequence[str],
    ) -> dict:
        """Accept or reject a pickup voucher by its concrete named signatures."""
        seal = self.get_seal(seal_id)
        scheme = self.get_scheme(scheme_id)
        settlement_no = require_text(settlement_no, "settlement_no")
        shift_id = require_text(shift_id, "shift_id")
        sigs = _seat_list(signatures, "signatures")

        base = {
            "seal_id": seal_id,
            "scheme_id": scheme_id,
            "shift_id": shift_id,
            "settlement_no": settlement_no,
            "signatures": sigs,
            "accepted": False,
            "qualifying_groups": [],
            "reason": None,
        }
        if scheme["seal_id"] != seal_id:
            base["reason"] = "scheme does not belong to this seal"
            return base
        if not scheme["feasible"]:
            base["reason"] = "scheme is not feasible"
            return base

        plan = scheme["plan"]
        shift = next(
            (s for s in plan["shifts"] if s["shift_id"] == shift_id), None
        )
        if shift is None:
            base["reason"] = f"unknown shift {shift_id!r} for this scheme"
            return base
        on_duty = frozenset(shift["on_duty"])

        stale = self.seal_staleness(seal_id)
        if stale["requires_reseal"]:
            if stale["non_reserved_runs"]:
                detail = ", ".join(
                    f"{r['run_id']} is {r['state']!r}"
                    for r in stale["non_reserved_runs"]
                )
                base["reason"] = (
                    f"delivery forbidden: sealed run no longer 'reserved' ({detail})"
                )
            elif stale["changed_runs"] or stale["structural_change"]:
                base["reason"] = (
                    "seal is stale (reservations changed); re-seal before delivery"
                )
            else:
                base["reason"] = "seal is stale; re-seal before delivery"
            base["staleness"] = stale
            return base

        groups_by_name = {
            g["name"]: cosign.Group(g["name"], frozenset(g["seats"]), g["fee"])
            for g in scheme["groups"]
        }
        activated = plan["activated_groups"]
        qualifying = []
        for name in activated:
            group = groups_by_name[name]
            if not (group.seats <= on_duty):
                continue
            if group.seats <= set(sigs):
                qualifying.append(name)
        base["qualifying_groups"] = qualifying
        base["per_shift"] = plan["per_shift"].get(shift_id, [])

        # ledger-level uniqueness: one settlement per seal
        existing = self.conn.execute(
            "SELECT settlement_no FROM postings WHERE seal_id = ?", (seal_id,)
        ).fetchone()
        if existing is not None and existing["settlement_no"] != settlement_no:
            base["reason"] = (
                f"seal already settled under different settlement number "
                f"{existing['settlement_no']!r}"
            )
            base["already_settled"] = existing["settlement_no"]
            return base

        if not qualifying:
            base["reason"] = (
                "no activated group is both fully on duty for this shift and "
                "carries every member's signature"
            )
            return base
        base["accepted"] = True
        base["reason"] = "accepted"
        return base

    def post_settlement(
        self,
        seal_id: str,
        scheme_id: str,
        shift_id: str,
        settlement_no: str,
        signatures: Sequence[str],
        day: int,
    ) -> dict:
        """Formally book one settlement; dedupe by (seal, settlement number)."""
        with transaction(self.conn):
            advance_day(self.conn, day)
            settlement_no = require_text(settlement_no, "settlement_no")

            # Deduplicate first: replaying the same (seal, settlement number)
            # returns the stored posting verbatim, even if stock has moved on.
            existing = self.conn.execute(
                "SELECT * FROM postings WHERE seal_id = ?", (seal_id,)
            ).fetchone()
            if existing is not None:
                if existing["settlement_no"] == settlement_no:
                    return self._posting_view(existing, replayed=True)
                raise Conflict(
                    f"seal {seal_id} already has a settlement under a different "
                    f"number ({existing['settlement_no']})"
                )

            receipt = self.verify_voucher(
                seal_id, scheme_id, shift_id, settlement_no, signatures
            )
            if not receipt["accepted"]:
                return {"posted": False, **receipt}

            # delivery is forbidden once a sealed run has started or released
            seal = self.get_seal(seal_id)
            for rid in seal["closure_runs"]:
                row = self.conn.execute(
                    "SELECT state FROM runs WHERE run_id = ?", (rid,)
                ).fetchone()
                if row is None or row["state"] != "reserved":
                    return {
                        "posted": False,
                        "seal_id": seal_id,
                        "settlement_no": settlement_no,
                        "accepted": False,
                        "reason": (
                            f"run {rid!r} is {None if row is None else row['state']}, "
                            "not 'reserved'; delivery is forbidden"
                        ),
                    }

            existing = self.conn.execute(
                "SELECT * FROM postings WHERE seal_id = ?", (seal_id,)
            ).fetchone()
            if existing is not None:
                raise Conflict(
                    f"seal {seal_id} already has a settlement under a different "
                    f"number ({existing['settlement_no']})"
                )

            chosen = receipt["qualifying_groups"][0]
            posting_id = f"{seal_id}::{settlement_no}"
            try:
                self.conn.execute(
                    "INSERT INTO postings(posting_id, seal_id, scheme_id, "
                    "settlement_no, group_name, signatures_json, posted_day) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        posting_id,
                        seal_id,
                        scheme_id,
                        settlement_no,
                        chosen,
                        _dumps(sorted(set(receipt["signatures"]))),
                        day,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = self.conn.execute(
                    "SELECT * FROM postings WHERE seal_id = ?", (seal_id,)
                ).fetchone()
                if existing is not None and existing["settlement_no"] == settlement_no:
                    return self._posting_view(existing, replayed=True)
                raise Conflict(f"seal {seal_id} is already settled")
            row = self.conn.execute(
                "SELECT * FROM postings WHERE posting_id = ?", (posting_id,)
            ).fetchone()
            return self._posting_view(row, replayed=False)

    def get_posting(self, seal_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM postings WHERE seal_id = ?", (seal_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"seal {seal_id!r} has no settlement")
        return self._posting_view(row, replayed=False)

    def _posting_view(self, row: sqlite3.Row, replayed: bool) -> dict:
        return {
            "posted": True,
            "posting_id": row["posting_id"],
            "seal_id": row["seal_id"],
            "scheme_id": row["scheme_id"],
            "settlement_no": row["settlement_no"],
            "group": row["group_name"],
            "signatures": json.loads(row["signatures_json"]),
            "posted_day": row["posted_day"],
            "replayed": replayed,
        }

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _seal_row(self, seal_id: str) -> sqlite3.Row:
        require_text(seal_id, "seal_id")
        row = self.conn.execute(
            "SELECT * FROM seals WHERE seal_id = ?", (seal_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown seal {seal_id!r}")
        return row

    def _require_run(self, run_id: str) -> None:
        if self.conn.execute(
            "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone() is None:
            raise NotFoundError(f"unknown run {run_id!r}")

    def _is_run(self, holder_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (holder_id,)
            ).fetchone()
            is not None
        )

    def _active_by_holder(self) -> dict[str, list[sqlite3.Row]]:
        rows = self.conn.execute(
            "SELECT * FROM reservations WHERE status = 'active' "
            "ORDER BY reservation_id"
        ).fetchall()
        out: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            out.setdefault(row["holder_id"], []).append(row)
        return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _parse_groups(raw: Sequence[Mapping]) -> list[cosign.Group]:
    try:
        return cosign.parse_groups(raw)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def _parse_shifts(raw: Sequence[Mapping]) -> list[cosign.Shift]:
    try:
        return cosign.parse_shifts(raw)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def _seat_set(seats: Sequence[str], where: str) -> frozenset[str]:
    return frozenset(_seat_list(seats, where))


def _seat_list(seats: Sequence[str], where: str) -> list[str]:
    if not isinstance(seats, (list, tuple, set, frozenset)):
        raise ValidationError(f"{where} must be a list of seat ids")
    out: list[str] = []
    seen: set[str] = set()
    for seat in seats:
        seat = require_text(seat, "seat_id")
        if seat in seen:
            raise ValidationError(f"{where}: duplicate seat {seat!r}")
        seen.add(seat)
        out.append(seat)
    if not out:
        raise ValidationError(f"{where} must not be empty")
    return out


def _dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _normalize(rows: Sequence[Mapping]) -> list[tuple]:
    return sorted(
        (r["reservation_id"], r["bottle_id"], int(r["portions"]), r["status"])
        for r in rows
    )


def _portions_by_bottle(rows: Sequence[Mapping]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        out[row["bottle_id"]] = out.get(row["bottle_id"], 0) + int(row["portions"])
    return dict(sorted(out.items()))


def _fingerprint(
    bottle_facts: Mapping[str, Mapping],
    runs_out: Mapping[str, Mapping],
    snapshot: Mapping[str, Sequence[Mapping]],
) -> str:
    payload = {
        "bottles": {
            bid: {
                "total_portions": facts["total_portions"],
                "consumed": facts["consumed"],
                "reserved": facts["reserved"],
                "unallocated": facts["unallocated"],
                "ordinary_holders": facts["ordinary_holders"],
                "runs": facts["runs"],
            }
            for bid, facts in bottle_facts.items()
        },
        "runs": {
            rid: {
                "state": info["state"],
                "portions_by_bottle": info["portions_by_bottle"],
            }
            for rid, info in runs_out.items()
        },
        "snapshot": {rid: _normalize(rows) for rid, rows in snapshot.items()},
    }
    return _dumps(payload)
