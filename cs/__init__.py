"""Independent classification catalog and fact summary store.

Public entry points:

* :mod:`cs.catalog` - pure DAG algorithms (cycles, ancestors, descendants,
  coverage explanation).
* :mod:`cs.store` - SQLite-backed catalog and fact store with atomic bundles
  and cut-pinned queries.
* :mod:`cs.split` - exact rational per-ticket flow to receiving cabinets.
* :mod:`cs.settle` - whole-cent settlement with minimal upward-rounding fee.
* :mod:`cs.cli` - command line interface.
"""

from . import settle, split  # noqa: F401
from .store import (  # noqa: F401
    CatalogError,
    CatalogState,
    Committed,
    ConflictError,
    Cut,
    Fact,
    FactError,
    ProductError,
    Store,
    StoreError,
    ingest_operation,
)

__all__ = [
    "CatalogError",
    "CatalogState",
    "Committed",
    "ConflictError",
    "Cut",
    "Fact",
    "FactError",
    "ProductError",
    "Store",
    "StoreError",
    "ingest_operation",
    "settle",
    "split",
]
