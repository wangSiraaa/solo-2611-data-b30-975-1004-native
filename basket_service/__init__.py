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
from .repair import RepairShop
from .warranty import WarrantyService


class Service:
    """Thin façade wiring the quote service and ledger onto one database."""

    def __init__(self, path: str = ":memory:", *, busy_timeout_ms: int = 30000):
        self.db = Database(path, busy_timeout_ms=busy_timeout_ms)
        self.quotes = QuoteService(self.db)
        self.ledger = AfterSalesLedger(self.db)
        self.warranties = WarrantyService(self.db)
        self.repairs = RepairShop(self.db)

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

    # warranty surface ----------------------------------------------------
    def publish_warranty_term(self, term_id, sku, base_days, *, coupon_id=None, bonus_days=0):
        return self.warranties.publish_term(term_id, sku, base_days, coupon_id=coupon_id, bonus_days=bonus_days)

    def enroll_warranties(self, sale_id, sold_day, *, term_map=None):
        return self.warranties.enroll_sale(sale_id, sold_day, term_map=term_map)

    def get_warranty(self, sale_id, unit_id):
        return self.warranties.get_warranty(sale_id, unit_id)

    def list_warranties(self, sale_id):
        return self.warranties.list_warranties(sale_id)

    def register_outage(self, record_id, sale_id, unit_id, start_day, end_day, *, registered_day=None, note=""):
        return self.warranties.register_outage(
            record_id, sale_id, unit_id, start_day, end_day, registered_day=registered_day, note=note
        )

    def get_outage(self, record_id):
        return self.warranties.get_outage(record_id)

    def list_outages(self, sale_id, unit_id):
        return self.warranties.list_outages(sale_id, unit_id)

    def mark_return_exit(self, sale_id, unit_id, return_day, return_request_id):
        return self.warranties.mark_return_exit(sale_id, unit_id, return_day, return_request_id)

    def query_warranty(self, query_id, sale_id, unit_id, query_day):
        return self.warranties.query_warranty(query_id, sale_id, unit_id, query_day)

    def get_warranty_query(self, query_id):
        return self.warranties.get_query(query_id)

    def warranty_versions(self, sale_id, unit_id):
        return self.warranties.versions(sale_id, unit_id)

    def warranty_version_summary(self, sale_id, unit_id, version):
        return self.warranties.get_version_summary(sale_id, unit_id, version)

    # repair shop surface -------------------------------------------------
    def register_repair_method(self, method_id, sku, duration_days, specialty):
        return self.repairs.register_method(method_id, sku, duration_days, specialty)

    def register_technician(self, technician_id, specialty, *, unavailable=None):
        return self.repairs.register_technician(technician_id, specialty, unavailable=unavailable)

    def open_work_order(self, work_order_id, sale_id, unit_id, delivery_day, repair_value, method_ids):
        return self.repairs.open_work_order(work_order_id, sale_id, unit_id, delivery_day, repair_value, method_ids)

    def get_work_order(self, work_order_id):
        return self.repairs.get_work_order(work_order_id)

    def plan_repairs(self, batch_id, work_order_ids, horizon_end_day):
        return self.repairs.plan_batch(batch_id, work_order_ids, horizon_end_day)

    def get_repair_batch(self, batch_id):
        return self.repairs.get_batch(batch_id)

    def get_booking_decision(self, work_order_id):
        return self.repairs.get_decision(work_order_id)

    def technician_calendar(self, technician_id):
        return self.repairs.technician_calendar(technician_id)

    # cross-cutting -------------------------------------------------------
    def run(self, plan):
        """Execute a JSON plan; see ``basket_service.planner``."""

        from .planner import run_plan

        return run_plan(self, plan)


def open_service(path: str = ":memory:", *, busy_timeout_ms: int = 30000) -> Service:
    return Service(path, busy_timeout_ms=busy_timeout_ms)


__all__ = ["Service", "open_service", "BasketError", "Database"]
