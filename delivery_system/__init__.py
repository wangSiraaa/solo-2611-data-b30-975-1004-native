"""Source-segment catalog + multilingual delivery repository.

Two deliberately separate sides:

* :class:`delivery_system.source_catalog.SourceCatalog` owns documents,
  stable segment identities, their order, current revisions and text.  It
  exposes an immutable :class:`SourceProduct` snapshot.
* :class:`delivery_system.delivery_repository.DeliveryRepository` owns
  translation entries and published packages.  It reads the *published source
  product* from the catalog and solves an exact, non-overlapping cover for one
  language at a time.

No translation or semantic scoring happens here: text is opaque.
"""

from .annotation_demo import run_annotation_demo
from .annotation_models import (
    BlockSpec,
    NoteDefinition,
    NoteReading,
    NoteRef,
    PageLayout,
    ProofSheet,
    SegmentNoteMarkers,
    TypesetResult,
)
from .annotations import (
    BlockInput,
    NoteRegistry,
    build_proof_sheet,
    render_page_sample,
    typeset,
)
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
from .source_catalog import SourceCatalog

__all__ = [
    "BlockInput",
    "BlockSpec",
    "CoverChunk",
    "CoverDiagnosis",
    "CoverResult",
    "DeliveryRepository",
    "NoteDefinition",
    "NoteReading",
    "NoteRef",
    "NoteRegistry",
    "PageLayout",
    "ProofSheet",
    "SegmentNoteMarkers",
    "SourceCatalog",
    "SourceProduct",
    "SourceRef",
    "SourceSegment",
    "TranslationAssessment",
    "TranslationEntry",
    "TypesetResult",
    "build_proof_sheet",
    "render_page_sample",
    "run_annotation_demo",
    "solve_cover",
    "typeset",
]
