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
from .cash import CashOffice
from .ledger import AfterSalesLedger
from .quote_service import QuoteService


class Service:
    """Thin façade wiring the quote service, ledger and cash office onto one database."""

    def __init__(self, path: str = ":memory:", *, busy_timeout_ms: int = 30000):
        self.db = Database(path, busy_timeout_ms=busy_timeout_ms)
        self.quotes = QuoteService(self.db)
        self.ledger = AfterSalesLedger(self.db)
        self.cash = CashOffice(self.db)

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

    def return_units(self, request_id: str, sale_id: str, unit_ids, grade, *, slot=None):
        return self.ledger.return_units(request_id, sale_id, unit_ids, grade, slot=slot)

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

    # cash office surface --------------------------------------------------
    def register_counter(self, counter_id: str):
        return self.cash.register_counter(counter_id)

    def list_counters(self):
        return self.cash.list_counters()

    def opening_balance(self, event_id: str, counter_id: str, denomination: int, qty: int):
        return self.cash.opening_balance(event_id, counter_id, denomination, qty)

    def register_bag(self, bag_id: str, coins, delivers_to, arrival_slot, freight):
        return self.cash.register_bag(bag_id, coins, delivers_to, arrival_slot, freight)

    def list_bags(self):
        return self.cash.list_bags()

    def counter_balance(self, counter_id=None):
        return self.cash.counter_balance(counter_id)

    def get_receipt(self, request_id: str):
        return self.cash.get_receipt(request_id)

    def list_receipts(self, sale_id=None):
        return self.cash.list_receipts(sale_id)

    def list_ious(self, status=None):
        return self.cash.list_ious(status)

    def get_iou(self, iou_id: str):
        return self.cash.get_iou(iou_id)

    def plan_day(self, *, node_budget=None):
        if node_budget is None:
            return self.cash.plan_day()
        return self.cash.plan_day(node_budget=node_budget)

    def save_plan(self, plan_id: str, plan):
        return self.cash.save_plan(plan_id, plan)

    def get_plan(self, plan_id: str):
        return self.cash.get_plan(plan_id)

    def apply_plan(self, plan=None, *, plan_id=None):
        return self.cash.apply_plan(plan, plan_id=plan_id)

    def pay_iou(self, iou_id: str, disbursement_id: str, coins=None):
        return self.cash.pay_iou(iou_id, disbursement_id, coins)

    def audit_cash(self):
        return self.cash.audit_cash()

    def close_day(self):
        return self.cash.close_day()

    # cross-cutting -------------------------------------------------------
    def run(self, plan):
        """Execute a JSON plan; see ``basket_service.planner``."""

        from .planner import run_plan

        return run_plan(self, plan)


def open_service(path: str = ":memory:", *, busy_timeout_ms: int = 30000) -> Service:
    return Service(path, busy_timeout_ms=busy_timeout_ms)


__all__ = ["Service", "open_service", "BasketError", "Database"]
