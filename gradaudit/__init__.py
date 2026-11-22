"""gradaudit -- independent course-equivalence catalog and graduation audit library.

Public entry points
-------------------
* :func:`verify_plan_document` -- independent ``verify-plan`` (no solver).
* :func:`gradaudit.solver.solve` -- exact feasibility solver.
* :class:`Store` -- SQLite-backed catalog / records / requirements / audit
  preparation-confirmation library.

Typical CLI use::

    python -m gradaudit import-catalog catalog.json
    python -m gradaudit import-records records.json
    python -m gradaudit import-requirements requirements.json
    python -m gradaudit candidates X
    python -m gradaudit solve --catalog ... --records ... --requirements ...
    python -m gradaudit verify --plan plan_a.json --catalog ... --records ... --requirements ...
    python -m gradaudit prepare --plan ... --request REQ1 ...
    python -m gradaudit confirm --request REQ1
    python -m gradaudit status --request REQ1
    python -m gradaudit reopen --request REQ1 --reason "..."
    python -m gradaudit history --request REQ1
"""

from .errors import (
    ConfirmationError,
    ConfirmationStaleError,
    GradAuditError,
    NotPreparedError,
    RevisionConflictError,
    RevisionNotFoundError,
    SchemaError,
    VerificationError,
)
from .archive import (
    ARCHIVE_FORMAT,
    MAX_BOXES,
    Archive,
    Box,
    Copy,
    EvidencePage,
    RecordEvidence,
    parse_archive,
)
from .custody import (
    CUSTODY_FORMAT,
    CustodyStudent,
    custody_report,
    optimize_custody,
    parse_custody,
    review_retention,
)
from .library import Store
from .model import (
    Assignment,
    Catalog,
    Plan,
    Record,
    Records,
    Requirements,
    content_hash,
    parse_catalog,
    parse_plan,
    parse_records,
    parse_requirements,
)
from .solver import MasterResult, GroupOutcome, solve, single_group_max
from .verify import VerifyResult, verify_plan, verify_plan_document

__all__ = [
    "Store",
    "solve",
    "single_group_max",
    "MasterResult",
    "GroupOutcome",
    "verify_plan",
    "verify_plan_document",
    "VerifyResult",
    "Archive",
    "Box",
    "Copy",
    "EvidencePage",
    "RecordEvidence",
    "parse_archive",
    "ARCHIVE_FORMAT",
    "MAX_BOXES",
    "CustodyStudent",
    "parse_custody",
    "optimize_custody",
    "review_retention",
    "custody_report",
    "CUSTODY_FORMAT",
    "Catalog",
    "Records",
    "Requirements",
    "Plan",
    "Record",
    "Assignment",
    "parse_catalog",
    "parse_records",
    "parse_requirements",
    "parse_plan",
    "content_hash",
    "GradAuditError",
    "SchemaError",
    "RevisionNotFoundError",
    "RevisionConflictError",
    "VerificationError",
    "NotPreparedError",
    "ConfirmationError",
    "ConfirmationStaleError",
]

__version__ = "1.0.0"
