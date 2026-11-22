"""Independent basket pricing and after-sales ledger service.

Quick start::

    from basket_service import open_service

    svc = open_service("data/shop.db")
    quote = svc.quote(basket, coupons, request_id="q1", snapshot_id=snap)
    sale = svc.register_sale("S1", quote=quote)
    result = svc.return_units("r1", "S1", ["b"], {"b": "RESTOCK"})
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .db import Database
from .errors import BasketError
from .ledger import AfterSalesLedger
from .quote_service import QuoteService
from .sheet_service import SheetService


class Service:
    """Thin façade wiring the quote service, ledger and physical paper."""

    def __init__(self, path: str = ":memory:", *, busy_timeout_ms: int = 30000):
        self.db = Database(path, busy_timeout_ms=busy_timeout_ms)
        self.quotes = QuoteService(self.db)
        self.ledger = AfterSalesLedger(self.db)
        self.paper = SheetService(self.db)

    # quote surface -------------------------------------------------------
    def publish_snapshot(self, products=None, coupons=None, *, snapshot_id=None) -> str:
        return self.quotes.publish_snapshot(products, coupons, snapshot_id=snapshot_id)

    def get_snapshot(self, snapshot_id: str):
        return self.quotes.get_snapshot(snapshot_id)

    def quote(self, basket, coupons, request_id: Optional[str] = None, snapshot_id: Optional[str] = None):
        return self.quotes.quote(basket, coupons, request_id, snapshot_id)

    def get_quote(self, request_id: str):
        return self.quotes.get_quote(request_id)

    # ledger surface ------------------------------------------------------
    def register_sale(self, sale_id: str, **kwargs):
        return self.ledger.register_sale(sale_id, **kwargs)

    def get_sale(self, sale_id: str):
        return self.ledger.get_sale(sale_id)

    def return_units(self, request_id: str, sale_id: str, unit_ids, grade):
        return self.ledger.return_units(request_id, sale_id, unit_ids, grade)

    def get_return(self, request_id: str):
        return self.ledger.get_return(request_id)

    def adjust_inventory(self, event_id: str, sku: str, quantity: int, bucket: str):
        return self.ledger.adjust_inventory(event_id, sku, quantity, bucket)

    def inventory(self, skus=None):
        return self.ledger.inventory(skus)

    def audit_inventory(self):
        return self.ledger.audit_inventory()

    def audit_sales(self):
        return self.ledger.audit_sales()

    # physical paper surface ---------------------------------------------
    def register_sheet(self, sheet_id=None, **kwargs):
        return self.paper.register_sheet(sheet_id, **kwargs)

    def add_correction(self, fix_id, sheet_id, edges):
        return self.paper.add_correction(fix_id, sheet_id, edges)

    def get_sheet(self, sheet_id):
        return self.paper.get_sheet(sheet_id)

    def list_sheets(self):
        return self.paper.list_sheets()

    def try_tear(self, sheet_id, ticket_ids):
        return self.paper.try_tear(sheet_id, ticket_ids)

    def quote_group(self, group_id, baskets, **kwargs):
        return self.paper.quote_group(group_id, baskets, **kwargs)

    def get_group_quote(self, request_id):
        return self.paper.get_group_quote(request_id)

    def commit_group(self, request_id, sales):
        return self.paper.commit_group(request_id, sales)

    # cross-cutting -------------------------------------------------------
    def run(self, plan):
        """Execute a JSON plan; see ``basket_service.planner``."""

        from .planner import run_plan

        return run_plan(self, plan)


def open_service(path: str = ":memory:", *, busy_timeout_ms: int = 30000) -> Service:
    return Service(path, busy_timeout_ms=busy_timeout_ms)


__all__ = ["Service", "open_service", "BasketError", "Database"]
