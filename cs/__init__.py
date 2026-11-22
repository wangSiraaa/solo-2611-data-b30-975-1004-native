"""Independent classification catalog and fact summary store.

Public entry points:

* :mod:`cs.catalog` - pure DAG algorithms (cycles, ancestors, descendants,
  coverage explanation).
* :mod:`cs.ballot` - distance-only ballots and both-sides pair tallies.
* :mod:`cs.agenda` - cheapest legal knockout agenda that leaves a proposal.
* :mod:`cs.store` - SQLite-backed catalog and fact store with atomic bundles,
  cut-pinned queries, frozen ballot registers and agenda settlements.
* :mod:`cs.cli` - command line interface.
"""

from .agenda import AgendaError  # noqa: F401
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
    "AgendaError",
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
]
