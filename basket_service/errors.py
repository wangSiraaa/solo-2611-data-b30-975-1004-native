"""Typed errors for the basket quote and after-sales ledger service."""

from __future__ import annotations

from typing import Any, Dict, Optional


class BasketError(Exception):
    """A deterministic, machine readable failure.

    Every public operation either commits a complete result or raises one of
    these before any partially visible state is written.
    """

    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": self.message, "details": self.details}}

    def __repr__(self) -> str:  # pragma: no cover - convenience only
        return f"BasketError({self.code!r}, {self.message!r})"


# Input / contract failures -------------------------------------------------
INVALID_BASKET = "INVALID_BASKET"
INVALID_COUPON = "INVALID_COUPON"
INVALID_INPUT = "INVALID_INPUT"
INVALID_QUOTE = "INVALID_QUOTE"
INVALID_GRADE = "INVALID_GRADE"

# Quote service failures ----------------------------------------------------
UNKNOWN_SNAPSHOT = "UNKNOWN_SNAPSHOT"
SNAPSHOT_CONFLICT = "SNAPSHOT_CONFLICT"
REQUEST_CONFLICT = "REQUEST_CONFLICT"
QUOTE_CONFLICT = "QUOTE_CONFLICT"

# Ledger failures -----------------------------------------------------------
UNKNOWN_QUOTE = "UNKNOWN_QUOTE"
UNKNOWN_SALE = "UNKNOWN_SALE"
SALE_CONFLICT = "SALE_CONFLICT"
QUOTE_MISMATCH = "QUOTE_MISMATCH"
UNIT_INELIGIBLE = "UNIT_INELIGIBLE"
UNIT_ALREADY_RETURNED = "UNIT_ALREADY_RETURNED"
OVERLAPPING_RETURN = "OVERLAPPING_RETURN"
INVENTORY_CONFLICT = "INVENTORY_CONFLICT"

# Physical double-sided coupon sheets --------------------------------------
INVALID_SHEET = "INVALID_SHEET"
INVALID_TICKET = "INVALID_TICKET"
INVALID_PERFORATION = "INVALID_PERFORATION"
UNKNOWN_SHEET = "UNKNOWN_SHEET"
SHEET_CONFLICT = "SHEET_CONFLICT"
TEAR_INFEASIBLE = "TEAR_INFEASIBLE"
PAPER_CONFLICT = "PAPER_CONFLICT"
INVALID_GROUP = "INVALID_GROUP"
GROUP_CONFLICT = "GROUP_CONFLICT"
UNKNOWN_GROUP = "UNKNOWN_GROUP"
NO_FEASIBLE_PLAN = "NO_FEASIBLE_PLAN"
