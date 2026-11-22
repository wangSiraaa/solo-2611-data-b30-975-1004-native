"""Physical coupon-paper service: sheets, trials, group quotes and hand-overs.

The physical paper is stored per sheet as its *current piece decomposition*
(``paper_sheets.pieces_json``).  Nothing here trusts a caller's snapshot of the
paper: every mutation re-reads the pieces inside one ``BEGIN IMMEDIATE``
transaction, re-checks that all chosen tickets are still physically present and
simultaneously tearable, and only then records the hand-over and shrinks the
pieces.  So two counters racing for the same piece cannot both succeed -- the
loser sees the piece already gone and gets ``PAPER_CONFLICT``.

Trials (``try_tear`` / ``quote_group``) never write.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import paper, sheets
from .db import Database, canonical_json, deep_copy, json_loads, utc_now
from .errors import (
    GROUP_CONFLICT,
    INVALID_INPUT,
    PAPER_CONFLICT,
    SHEET_CONFLICT,
    TEAR_INFEASIBLE,
    UNKNOWN_GROUP,
    UNKNOWN_SHEET,
    BasketError,
)
from .paper import PaperState
from .sheets import Rect
from .validation import require_id


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class SheetService:
    def __init__(self, db: Database):
        self.db = db

    # -- sheet registration ------------------------------------------------
    def register_sheet(
        self,
        sheet_id: Optional[str],
        *,
        rows: Optional[int] = None,
        cols: Optional[int] = None,
        perforations: Optional[Sequence[Any]] = None,
        tickets: Optional[Sequence[Mapping[str, Any]]] = None,
        corrections: Optional[Sequence[Any]] = None,
        spec: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Publish one physical sheet.  Idempotent by content.

        Either pass ``spec`` (an object with ``rows``/``cols``/``tickets``/
        ``perforations``) or the fields directly.  Re-registering a different
        body under the same id is ``SHEET_CONFLICT``; the same body replays.
        """

        if spec is None:
            spec = {
                "sheet_id": sheet_id,
                "rows": rows,
                "cols": cols,
                "perforations": list(perforations or []),
                "tickets": list(tickets or []),
            }
        else:
            spec = dict(spec)
            if sheet_id is not None and spec.get("sheet_id") not in (None, sheet_id):
                raise BasketError(INVALID_INPUT, "sheet_id disagrees with spec", {"sheet_id": sheet_id})
            spec.setdefault("sheet_id", sheet_id)
        canonical = sheets.canonical_sheet(spec)
        corrections = list(corrections or [])
        # Validate corrections eagerly so a bad receipt fails before storage.
        sheets.effective_edges(canonical, corrections)
        spec_json = canonical_json(canonical)
        corrections_json = canonical_json(corrections)
        whole = [list(sheets.whole_sheet_rect(canonical["rows"], canonical["cols"]))]

        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM paper_sheets WHERE sheet_id = ?", (canonical["sheet_id"],)).fetchone()
            if row is not None:
                if row["spec_json"] != spec_json or row["corrections_json"] != corrections_json:
                    raise BasketError(SHEET_CONFLICT, "sheet already registered with different content", {"sheet_id": canonical["sheet_id"]})
                return self._sheet_result(conn, canonical["sheet_id"], repeated=True)
            count = conn.execute("SELECT COUNT(*) AS n FROM paper_sheets").fetchone()["n"]
            if count >= sheets.MAX_SHEETS_PER_EVENT:
                raise BasketError(
                    INVALID_INPUT,
                    f"an event holds at most {sheets.MAX_SHEETS_PER_EVENT} sheets",
                    {"limit": sheets.MAX_SHEETS_PER_EVENT},
                )
            conn.execute(
                "INSERT INTO paper_sheets (sheet_id, spec_json, corrections_json, pieces_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (canonical["sheet_id"], spec_json, corrections_json, canonical_json(whole), utc_now()),
            )
            return self._sheet_result(conn, canonical["sheet_id"], repeated=False)

    def add_correction(self, fix_id: str, sheet_id: str, edges: Sequence[Any]) -> Dict[str, Any]:
        """Record receipt-time missed perforations (add-only, idempotent)."""

        fix_id = require_id(fix_id, INVALID_INPUT, "fix_id")
        sheet_id = require_id(sheet_id, INVALID_INPUT, "sheet_id")
        edges = list(edges)
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT * FROM paper_perforation_fixes WHERE fix_id = ?", (fix_id,)).fetchone()
            content = canonical_json({"sheet_id": sheet_id, "edges": edges})
            if existing is not None:
                if existing["sheet_id"] != sheet_id or existing["edges_json"] != canonical_json(edges):
                    raise BasketError(SHEET_CONFLICT, "fix_id reused with different content", {"fix_id": fix_id})
                return self._sheet_result(conn, sheet_id, repeated=True)
            row = conn.execute("SELECT * FROM paper_sheets WHERE sheet_id = ?", (sheet_id,)).fetchone()
            if row is None:
                raise BasketError(UNKNOWN_SHEET, "unknown sheet", {"sheet_id": sheet_id})
            sheet = json_loads(row["spec_json"])
            corrections = json_loads(row["corrections_json"]) + edges
            sheets.effective_edges(sheet, corrections)  # validate the merged set
            conn.execute(
                "INSERT INTO paper_perforation_fixes (fix_id, sheet_id, edges_json, created_at) VALUES (?, ?, ?, ?)",
                (fix_id, sheet_id, canonical_json(edges), utc_now()),
            )
            conn.execute(
                "UPDATE paper_sheets SET corrections_json = ? WHERE sheet_id = ?",
                (canonical_json(corrections), sheet_id),
            )
            return self._sheet_result(conn, sheet_id, repeated=False)

    def get_sheet(self, sheet_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM paper_sheets WHERE sheet_id = ?", (sheet_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_SHEET, "unknown sheet", {"sheet_id": sheet_id})
        return self._sheet_payload(row)

    def list_sheets(self) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute("SELECT * FROM paper_sheets ORDER BY sheet_id").fetchall()
        return [self._sheet_payload(row) for row in rows]

    def _sheet_payload(self, row) -> Dict[str, Any]:
        spec = json_loads(row["spec_json"])
        corrections = json_loads(row["corrections_json"])
        pieces = json_loads(row["pieces_json"])
        state = PaperState.from_json(spec, corrections, pieces)
        return {
            "sheet_id": spec["sheet_id"],
            "rows": spec["rows"],
            "cols": spec["cols"],
            "perforations": list(spec["perforations"]),
            "corrections": corrections,
            "effective_perforations": sorted(sheets.edge_str(e) for e in state.edges),
            "tickets": deep_copy(spec["tickets"]),
            "pieces": pieces,
            "remaining_tickets": [
                {"ticket_id": t["ticket_id"], "side": t["side"], "coupon_id": t["coupon_id"]}
                for t in spec["tickets"]
                if state.owner(tuple(t["rect"])) is not None
            ],
        }

    def _sheet_result(self, conn, sheet_id: str, *, repeated: bool) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM paper_sheets WHERE sheet_id = ?", (sheet_id,)).fetchone()
        payload = self._sheet_payload(row)
        payload["ok"] = True
        payload["repeated"] = repeated
        return payload

    # -- customer trial ----------------------------------------------------
    def try_tear(self, sheet_id: str, ticket_ids: Sequence[str]) -> Dict[str, Any]:
        """Tell a customer whether this set of tickets can be taken together.

        Pure trial: reads current paper and returns the real ordered tear
        sequence (or ``TEAR_INFEASIBLE``).  Unused corners may be discarded.
        """

        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM paper_sheets WHERE sheet_id = ?", (sheet_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_SHEET, "unknown sheet", {"sheet_id": sheet_id})
        spec = json_loads(row["spec_json"])
        state = PaperState.from_json(spec, json_loads(row["corrections_json"]), json_loads(row["pieces_json"]))
        targets = paper.submission_rects(spec, list(ticket_ids))
        steps = paper.tear_plan(state, targets)
        if steps is None:
            raise BasketError(
                TEAR_INFEASIBLE,
                "these tickets cannot be torn out together",
                {"sheet_id": sheet_id, "ticket_ids": list(ticket_ids)},
            )
        applied = paper.apply_tears(state, targets)
        remaining = applied[0] if applied else state.to_json()
        return {
            "ok": True,
            "sheet_id": sheet_id,
            "ticket_ids": sorted(ticket_ids),
            "rects": [list(t) for t in targets],
            "steps": steps,
            "projected_remaining": [list(p) for p in remaining],
        }

    # -- group quote (trial) ----------------------------------------------
    def quote_group(
        self,
        group_id: str,
        baskets: Sequence[Mapping[str, Any]],
        *,
        request_id: Optional[str] = None,
        accepted_coupons: Optional[Sequence[str]] = None,
        store: bool = True,
    ) -> Dict[str, Any]:
        """Minimise the combined payment for simultaneously paid baskets.

        This is a trial: it never moves paper.  Passing ``store=True`` (the
        default) memoises the trial under ``request_id`` so the same trial
        replays byte-for-byte.
        """

        group_id = require_id(group_id, INVALID_INPUT, "group_id")
        states = self._load_states()
        plan = paper.plan_group(baskets, states, accepted_coupons=accepted_coupons)
        result = {
            "ok": True,
            "group_id": group_id,
            "baskets": plan["baskets"],
            "handovers": plan["handovers"],
            "tears": plan["tears"],
            "projected_remaining": plan["projected_remaining"],
            "original_total": plan["original_total"],
            "total_paid": plan["total_paid"],
            "total_discount": plan["total_discount"],
        }
        if not store:
            return result
        request_id = request_id or self._group_request_id(group_id, baskets, accepted_coupons)
        request_id = require_id(request_id, INVALID_INPUT, "request_id")
        material = {
            "group_id": group_id,
            "baskets": [{"basket_id": b["basket_id"], "units": b["units"]} for b in _sorted_baskets(baskets)],
            "accepted_coupons": list(accepted_coupons) if accepted_coupons is not None else None,
            "paper": self._paper_fingerprint(),
        }
        basket_sig = _hash(material)
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT * FROM paper_groups WHERE request_id = ?", (request_id,)).fetchone()
            if existing is not None:
                if existing["group_id"] != group_id or existing["basket_sig"] != basket_sig:
                    raise BasketError(GROUP_CONFLICT, "request_id reused with a different group trial", {"request_id": request_id})
                return json_loads(existing["quote_json"])
            if result["total_paid"] < 0:  # pragma: no cover - discounts never exceed price
                raise BasketError(INVALID_GROUP, "negative basket payment", {})
            stored = dict(result)
            stored["request_id"] = request_id
            conn.execute(
                "INSERT INTO paper_groups (request_id, group_id, basket_sig, quote_json, registered, created_at) VALUES (?, ?, ?, ?, 0, ?)",
                (request_id, group_id, basket_sig, canonical_json(stored), utc_now()),
            )
        return deep_copy(stored)

    def get_group_quote(self, request_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT quote_json FROM paper_groups WHERE request_id = ?", (request_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_GROUP, "unknown group trial", {"request_id": request_id})
        return json_loads(row["quote_json"])

    def commit_group(self, request_id: str, sales: Mapping[str, str]) -> Dict[str, Any]:
        """Commit a stored trial: hand over every piece and register each sale.

        ``sales`` maps ``basket_id -> sale_id`` for *every* basket in the trial.
        The full quote of each basket is registered with the ledger, so each
        basket keeps its own official per-unit paid amounts.  All hand-overs,
        sale registrations and paper shrink happen in one transaction; if the
        paper has changed since the trial (another counter), nothing commits and
        ``PAPER_CONFLICT`` is raised.
        """

        require_id(request_id, INVALID_INPUT, "request_id")
        if not isinstance(sales, Mapping) or not sales:
            raise BasketError(INVALID_INPUT, "sales must be a non-empty basket_id -> sale_id mapping")
        sales = {require_id(k, INVALID_INPUT, "basket_id"): require_id(v, INVALID_INPUT, "sale_id") for k, v in sales.items()}

        from .ledger import AfterSalesLedger

        ledger = AfterSalesLedger(self.db)
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM paper_groups WHERE request_id = ?", (request_id,)).fetchone()
            if row is None:
                raise BasketError(UNKNOWN_GROUP, "unknown group trial", {"request_id": request_id})
            stored = json_loads(row["quote_json"])
            basket_ids = [b["basket_id"] for b in stored["baskets"]]
            if set(sales) != set(basket_ids):
                raise BasketError(INVALID_INPUT, "sales must name exactly the trial's baskets", {"baskets": basket_ids})
            if row["registered"]:
                prior = self._group_commit_result(conn, request_id, stored)
                if prior["sales"] != sales:
                    raise BasketError(GROUP_CONFLICT, "group already committed with different sale ids", {"request_id": request_id})
                prior["repeated"] = True
                return prior

            # Re-validate paper and re-plan against the *current* pieces inside
            # this write transaction.  This is what makes losing a race safe.
            states = self._load_states(conn)
            paper_check = self._recheck_plan(stored, states)
            if paper_check is None:
                raise BasketError(
                    PAPER_CONFLICT,
                    "the paper changed since this trial; re-quote against current pieces",
                    {"request_id": request_id},
                )
            tears, remaining = paper_check

            # Register each basket's official quote.  The ledger validates the
            # payload structurally and never re-optimises.
            sale_results = []
            for basket in stored["baskets"]:
                sale_id = sales[basket["basket_id"]]
                sale = ledger.register_sale(sale_id, quote=basket["quote"], _in_tx=conn)
                sale_results.append(sale)

            for sheet_id, steps in tears.items():
                conn.execute(
                    "INSERT INTO paper_consumptions (request_id, group_id, sheet_id, before_json, after_json, steps_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        request_id,
                        stored["group_id"],
                        sheet_id,
                        canonical_json(states[sheet_id].to_json()),
                        canonical_json(remaining[sheet_id]),
                        canonical_json(steps),
                        utc_now(),
                    ),
                )
                conn.execute(
                    "UPDATE paper_sheets SET pieces_json = ? WHERE sheet_id = ?",
                    (canonical_json(remaining[sheet_id]), sheet_id),
                )
            for handover in stored["handovers"]:
                conn.execute(
                    "INSERT INTO paper_handovers (request_id, sheet_id, basket_id, rect_json, ticket_ids_json, coupon_ids_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        request_id,
                        handover["sheet_id"],
                        handover["basket_id"],
                        canonical_json(handover["rect"]),
                        canonical_json(handover["consumes_ticket_ids"]),
                        canonical_json([handover["coupon_id"]]),
                        utc_now(),
                    ),
                )
            for basket_id, sale_id in sorted(sales.items()):
                conn.execute(
                    "INSERT INTO paper_group_sales (request_id, basket_id, sale_id) VALUES (?, ?, ?)",
                    (request_id, basket_id, sale_id),
                )
            conn.execute("UPDATE paper_groups SET registered = 1 WHERE request_id = ?", (request_id,))
            result = self._group_commit_result(conn, request_id, stored)
            result["repeated"] = False
            return result

    def _recheck_plan(self, stored: Mapping[str, Any], states: Mapping[str, PaperState]):
        """Verify the stored hand-overs are still possible; return tears/remainder.

        We do not trust the trial's tear steps: we re-run the tear search on the
        current pieces for exactly the stored target rectangles.  If any piece
        is gone (another counter), this returns ``None``.
        """

        targets_by_sheet: Dict[str, List[Rect]] = {}
        for handover in stored["handovers"]:
            sheet_id = handover["sheet_id"]
            if sheet_id not in states:
                return None
            rect = tuple(handover["rect"])
            if handover["ticket_id"] not in {t["ticket_id"] for t in states[sheet_id].sheet["tickets"]}:
                # Guard against malformed stored data.
                return None
            target_ticket = next(t for t in states[sheet_id].sheet["tickets"] if t["ticket_id"] == handover["ticket_id"])
            if tuple(target_ticket["rect"]) != rect:
                return None
            if states[sheet_id].owner(rect) is None:
                return None
            targets_by_sheet.setdefault(sheet_id, []).append(rect)
        tears: Dict[str, List[Dict[str, Any]]] = {}
        remaining: Dict[str, List[List[int]]] = {}
        for sheet_id, rects in targets_by_sheet.items():
            steps = paper.tear_plan(states[sheet_id], rects)
            if steps is None:
                return None
            applied = paper.apply_tears(states[sheet_id], rects)
            if applied is None:
                return None
            tears[sheet_id] = steps
            remaining[sheet_id] = applied[0]
        return tears, remaining

    def _group_commit_result(self, conn, request_id: str, stored: Mapping[str, Any]) -> Dict[str, Any]:
        rows = conn.execute(
            "SELECT basket_id, sale_id FROM paper_group_sales WHERE request_id = ? ORDER BY basket_id",
            (request_id,),
        ).fetchall()
        sales = {row["basket_id"]: row["sale_id"] for row in rows}
        handovers = [
            dict(row)
            for row in conn.execute(
                "SELECT sheet_id, basket_id, rect_json, ticket_ids_json, coupon_ids_json FROM paper_handovers WHERE request_id = ? ORDER BY seq",
                (request_id,),
            ).fetchall()
        ]
        for handover in handovers:
            handover["rect"] = json_loads(handover.pop("rect_json"))
            handover["consumes_ticket_ids"] = json_loads(handover.pop("ticket_ids_json"))
            handover["coupon_ids"] = json_loads(handover.pop("coupon_ids_json"))
        return {
            "ok": True,
            "request_id": request_id,
            "group_id": stored["group_id"],
            "sales": sales,
            "handovers": handovers,
            "total_paid": stored["total_paid"],
            "basket_totals": {b["basket_id"]: b["total"] for b in stored["baskets"]},
            "sheets": self._current_pieces(conn),
            "repeated": False,
        }

    # -- helpers -----------------------------------------------------------
    def _load_states(self, conn=None) -> Dict[str, PaperState]:
        if conn is None:
            with self.db.connection() as own:
                return self._load_states(own)
        rows = conn.execute("SELECT * FROM paper_sheets ORDER BY sheet_id").fetchall()
        return {
            row["sheet_id"]: PaperState.from_json(
                json_loads(row["spec_json"]),
                json_loads(row["corrections_json"]),
                json_loads(row["pieces_json"]),
            )
            for row in rows
        }

    def _paper_fingerprint(self) -> List[Any]:
        return [
            {"sheet_id": row["sheet_id"], "pieces": json_loads(row["pieces_json"]), "corrections": json_loads(row["corrections_json"])}
            for row in self._load_rows()
        ]

    def _load_rows(self):
        with self.db.connection() as conn:
            return conn.execute("SELECT * FROM paper_sheets ORDER BY sheet_id").fetchall()

    def _current_pieces(self, conn) -> Dict[str, List[List[int]]]:
        rows = conn.execute("SELECT sheet_id, pieces_json FROM paper_sheets ORDER BY sheet_id").fetchall()
        return {row["sheet_id"]: json_loads(row["pieces_json"]) for row in rows}

    def _group_request_id(self, group_id: str, baskets: Sequence[Mapping[str, Any]], accepted_coupons: Optional[Sequence[str]]) -> str:
        material = {
            "group_id": group_id,
            "baskets": [{"basket_id": b["basket_id"], "units": b["units"]} for b in _sorted_baskets(baskets)],
            "accepted_coupons": list(accepted_coupons) if accepted_coupons is not None else None,
            "paper": self._paper_fingerprint(),
        }
        return "paper-" + _hash(material)[:24]


def _sorted_baskets(baskets: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    return sorted(baskets, key=lambda b: b["basket_id"])


__all__ = ["SheetService"]
