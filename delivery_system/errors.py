"""Domain errors with actionable messages."""

from __future__ import annotations

from typing import Optional


class DeliveryError(Exception):
    """Base class for expected domain failures."""


class DocumentExists(DeliveryError):
    pass


class DocumentNotFound(DeliveryError):
    pass


class SegmentNotFound(DeliveryError):
    pass


class DuplicateSegment(DeliveryError):
    pass


class InvalidPosition(DeliveryError):
    pass


class RevisionConflict(DeliveryError):
    pass


class TranslationExists(DeliveryError):
    pass


class InvalidTranslation(DeliveryError):
    pass


class PackageExistsImmutable(DeliveryError):
    """A package id was reused with different frozen content."""


class NoCoverError(DeliveryError):
    """Publishing was attempted but no exact current cover exists."""

    def __init__(self, message: str, result: Optional[object] = None) -> None:
        super().__init__(message)
        self.result = result


class AnnotationError(DeliveryError):
    """Base class for proof-sheet / annotation registry failures."""


class DanglingNoteRef(AnnotationError):
    """A marker points at a note id/edition that was never registered."""


class NoteDependencyCycle(AnnotationError):
    """Expanding note dependencies revisits a note already on the path."""


class IncompatibleNoteEdition(AnnotationError):
    """One delivery block asks for two different editions of the same note id."""


class MissingMandatoryNote(AnnotationError):
    """A covered segment revision requires a marker that the block omits."""


class InfeasibleLayout(AnnotationError):
    """No pagination keeps every block whole within the page/page-count limits."""
