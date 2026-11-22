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

# Warranty failures ---------------------------------------------------------
UNKNOWN_TERM = "UNKNOWN_TERM"
TERM_CONFLICT = "TERM_CONFLICT"
UNKNOWN_WARRANTY = "UNKNOWN_WARRANTY"
WARRANTY_CONFLICT = "WARRANTY_CONFLICT"
UNKNOWN_COUPON_TERM = "UNKNOWN_COUPON_TERM"
UNKNOWN_RECORD = "UNKNOWN_RECORD"
RECORD_CONFLICT = "RECORD_CONFLICT"
INVALID_INTERVAL = "INVALID_INTERVAL"
RETURN_CONFLICT = "RETURN_CONFLICT"

# Repair shop failures ------------------------------------------------------
UNKNOWN_TECHNICIAN = "UNKNOWN_TECHNICIAN"
UNKNOWN_METHOD = "UNKNOWN_METHOD"
UNKNOWN_BOOKING = "UNKNOWN_BOOKING"
BOOKING_CONFLICT = "BOOKING_CONFLICT"
UNKNOWN_WORK_ORDER = "UNKNOWN_WORK_ORDER"
WORK_ORDER_CONFLICT = "WORK_ORDER_CONFLICT"
INVALID_SPECIALTY = "INVALID_SPECIALTY"
INVALID_HORIZON = "INVALID_HORIZON"
