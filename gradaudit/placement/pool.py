"""Pooling students whose catalogs live in their own local libraries.

Each student brings their own gradaudit :class:`~gradaudit.Store` (their own
catalog revisions, transfer records and requirements).  The pool keeps those
local libraries separate -- a mapping change in one student's catalog never
touches another student's -- while offering one place to:

* run a joint eligibility review for one student x one post;
* re-verify an issued review against that same student's local documents;
* collect every approved ``(student, post)`` pair to feed the station matching.

Review documents are plain JSON-serializable dicts; callers may persist them
anywhere they like, and both verifiers can always re-check them from the local
libraries alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from ..library import Store
from ..model import Catalog, Records, Requirements
from .documents import Post
from .eligibility import ReviewResult, evaluate_eligibility
from .review_verify import ReviewVerifyResult, verify_review


@dataclass
class StudentLibrary:
    student_id: str
    store: Store
    catalog_revision: int
    records_revision: int
    requirements_revision: int

    def catalog(self) -> Catalog:
        doc = self.store.get_catalog(self.catalog_revision)
        if doc is None:
            raise KeyError(f"catalog revision {self.catalog_revision} missing for {self.student_id}")
        return doc

    def records(self) -> Records:
        doc = self.store.get_records(self.records_revision)
        if doc is None:
            raise KeyError(f"records revision {self.records_revision} missing for {self.student_id}")
        return doc

    def requirements(self) -> Requirements:
        doc = self.store.get_requirements(self.requirements_revision)
        if doc is None:
            raise KeyError(
                f"requirements revision {self.requirements_revision} missing for {self.student_id}"
            )
        return doc


class ReviewPool:
    """Aggregates per-student local libraries for department-side reviews."""

    def __init__(self) -> None:
        self._students: Dict[str, StudentLibrary] = {}
        self._reviews: List[dict] = []

    def register_student(self, library: StudentLibrary) -> None:
        if library.student_id in self._students:
            raise ValueError(f"student {library.student_id!r} is already registered")
        # Fail fast on inconsistent student identity inside the local library.
        records = library.records()
        if records.student_id != library.student_id:
            raise ValueError(
                f"library student_id {records.student_id!r} disagrees with registration "
                f"{library.student_id!r}"
            )
        self._students[library.student_id] = library

    @property
    def student_ids(self) -> Tuple[str, ...]:
        return tuple(sorted(self._students))

    def review(self, student_id: str, post: Post) -> ReviewResult:
        """Department handling of one student's appeal for one post.

        Other posts' eligibility is unaffected: this pair gets its own
        reservation and graduation plan, and nothing is retained for posts the
        student did not appeal.
        """
        library = self._require(student_id)
        result = evaluate_eligibility(
            library.catalog(), library.records(), library.requirements(), post
        )
        self._reviews.append(result.as_document())
        return result

    def verify_review_document(self, review_doc: dict, posts: Dict[str, Post]) -> ReviewVerifyResult:
        library = self._require(review_doc["student_id"])
        return verify_review(
            review_doc,
            library.catalog(),
            library.records(),
            library.requirements(),
            posts,
        )

    def approved_pairs(self) -> Dict[str, frozenset]:
        out: Dict[str, set] = {}
        for doc in self._reviews:
            if doc["decision"] == "eligible":
                out.setdefault(doc["student_id"], set()).add(doc["post_id"])
        return {s: frozenset(t) for s, t in out.items()}

    def review_documents(self) -> List[dict]:
        return [dict(doc) for doc in self._reviews]

    def load_review_documents(self, docs: Sequence[dict]) -> None:
        self._reviews.extend(dict(d) for d in docs)

    def _require(self, student_id: str) -> StudentLibrary:
        try:
            return self._students[student_id]
        except KeyError:
            raise KeyError(f"student {student_id!r} has no registered local library") from None

    def close(self) -> None:
        for library in self._students.values():
            library.store.close()


__all__ = ["StudentLibrary", "ReviewPool"]
