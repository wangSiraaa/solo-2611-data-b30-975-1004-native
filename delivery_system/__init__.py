"""Source-segment catalog + multilingual delivery repository + rights desk.

Three deliberately separate sides:

* :class:`delivery_system.source_catalog.SourceCatalog` owns documents,
  stable segment identities, their order, current revisions and text.  It
  exposes an immutable :class:`SourceProduct` snapshot.
* :class:`delivery_system.delivery_repository.DeliveryRepository` owns
  translation entries and published packages.  It reads the *published source
  product* from the catalog and solves an exact, non-overlapping cover for one
  language at a time.
* :class:`delivery_system.rights_repository.RightsRepository` owns usage
  registrations against actual translation blocks, original grants, delegation
  chains, revocations and session tickets.

No translation or semantic scoring happens here: text is opaque.
"""

from .coverage import solve_cover
from .delivery_repository import DeliveryRepository
from .models import (
    CoverChunk,
    CoverDiagnosis,
    CoverResult,
    SourceProduct,
    SourceRef,
    SourceSegment,
    TranslationAssessment,
    TranslationEntry,
)
from .rights_engine import RightsEngine
from .rights_models import (
    BlockRef,
    ChainLink,
    ChainPath,
    Credential,
    Delegation,
    RequirementAtom,
    RootGrant,
    Scope,
    SigningAnswer,
    Ticket,
    TicketPlan,
    UsageRegistration,
)
from .rights_repository import RightsRepository
from .source_catalog import SourceCatalog

__all__ = [
    "BlockRef",
    "ChainLink",
    "ChainPath",
    "CoverChunk",
    "CoverDiagnosis",
    "CoverResult",
    "Credential",
    "Delegation",
    "DeliveryRepository",
    "RequirementAtom",
    "RightsEngine",
    "RightsRepository",
    "RootGrant",
    "Scope",
    "SigningAnswer",
    "SourceProduct",
    "SourceRef",
    "SourceSegment",
    "Ticket",
    "TicketPlan",
    "TranslationAssessment",
    "TranslationEntry",
    "UsageRegistration",
    "solve_cover",
]
