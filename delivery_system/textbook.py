"""Contrastive-textbook analysis, dictionary desk, and per-table tile packing.

A booklet built from a real translation package records, for every delivered
chunk, the *multiset of source words* covered by that chunk against the
*multiset of target words* in the chunk's text.  Both sides are
whitespace-separated word numbers and word order is irrelevant on either side:
only the per-word counts are kept ("对照课本只记整句的词数").

A document-wide dictionary is a bijection between the two equal-sized finite
vocabularies under which every chunk matches count for count.  Several such
dictionaries may exist (a book still fully symmetric on many words); sometimes
exactly one exists.  If none exists the booklet **contradicts itself** and must
not be signed for the classroom.

The tile board is a separate, pre-reveal problem.  Each practice sheet names a
table and a source-word multiset worth positive points; a table accepts at most
one sheet (equivalently, every sheet belongs to its own table).  The tiles are
**target-word** tiles: the retained published bundle(s) supply a fixed number of
copies of each target word and every physical tile may be packed into only one
table's bag.  Because the teacher packs the bags *before* revealing the real
dictionary, and the class must be able to work "under any dictionary compatible
with the book", a table's bag must contain, for every occurrence of a source
word, one copy of *each* target word that any compatible dictionary can map it
to.  This is exactly what produces the p/q story: the bundled chunk ``"x y"``
leaves ``p`` and ``q`` free to swap, so table ``p`` needs both kinds ``x`` and
``y``; with one copy of each in the store only one table can be served (best
3 points).  Splitting the chunk into single-segment entries fixes ``p->x`` and
``q->y`` so each table needs one kind and both can be served (5 points).

Public API
----------
Value objects:
    TextbookPair, Textbook, Exercise, TablePlan, Shortfall, AllocationResult
Building:
    build_book, analyze_package, verify_package_against_book
Dictionary desk:
    DictionarySpace (consistent/unique, image_of, check_pairs, all_dictionaries)
Tile board:
    package_supply, TileAllocationProblem, solve_allocation
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from .errors import (
    ExerciseContradiction,
    TextbookContradiction,
    TextbookError,
    TextbookLimitExceeded,
)
from .models import SourceProduct

MAX_DICTIONARY_PAIRS = 24
MAX_EXERCISES = 22


# ---------------------------------------------------------------------------
# small multiset helpers
# ---------------------------------------------------------------------------
def tokenize(text: str) -> tuple[str, ...]:
    """Whitespace-separated words; punctuation is not interpreted."""

    return tuple(text.split())


def frozen_multiset(words: Iterable[str]) -> tuple[tuple[str, int], ...]:
    """Deterministic, hashable ``(word, count)`` pairs sorted by word."""

    return tuple(sorted(Counter(words).items()))


# ---------------------------------------------------------------------------
# booklet (contrastive textbook)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TextbookPair:
    """One delivered chunk's paired source/target multisets.

    ``source_ids``/``source_revisions`` pin the pair to the exact source
    segments of the real package.  A chunk spanning several source segments is
    a *single* pair whose source side is the multiset union of everything it
    covered; it is never cut into two pairs.
    """

    translation_id: str
    source_ids: tuple[str, ...]
    source_revisions: tuple[int, ...]
    source_words: tuple[tuple[str, int], ...]
    target_words: tuple[tuple[str, int], ...]

    @property
    def source_multiset(self) -> Counter[str]:
        return Counter(dict(self.source_words))

    @property
    def target_multiset(self) -> Counter[str]:
        return Counter(dict(self.target_words))

    def to_dict(self) -> dict[str, object]:
        return {
            "translation_id": self.translation_id,
            "source_ids": list(self.source_ids),
            "source_revisions": list(self.source_revisions),
            "source_words": {w: c for w, c in self.source_words},
            "target_words": {w: c for w, c in self.target_words},
        }


@dataclass(frozen=True)
class Textbook:
    """A contrastive booklet built from one real, frozen translation package."""

    document_id: str
    language: str
    package_id: str
    content_sha256: str
    source_vocab: tuple[str, ...]
    target_vocab: tuple[str, ...]
    pairs: tuple[TextbookPair, ...]

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Textbook":
        pairs = tuple(
            TextbookPair(
                translation_id=str(p["translation_id"]),
                source_ids=tuple(str(s) for s in p["source_ids"]),
                source_revisions=tuple(int(r) for r in p["source_revisions"]),
                source_words=tuple(
                    (str(w), int(c))
                    for w, c in sorted(dict(p["source_words"]).items())
                ),
                target_words=tuple(
                    (str(w), int(c))
                    for w, c in sorted(dict(p["target_words"]).items())
                ),
            )
            for p in data["pairs"]
        )
        return cls(
            document_id=str(data["document_id"]),
            language=str(data["language"]),
            package_id=str(data["package_id"]),
            content_sha256=data.get("content_sha256", ""),
            source_vocab=tuple(str(w) for w in data["source_vocab"]),
            target_vocab=tuple(str(w) for w in data["target_vocab"]),
            pairs=pairs,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "language": self.language,
            "package_id": self.package_id,
            "content_sha256": self.content_sha256,
            "source_vocab": list(self.source_vocab),
            "target_vocab": list(self.target_vocab),
            "pairs": [p.to_dict() for p in self.pairs],
        }

    def pair_for(self, translation_id: str) -> Optional[TextbookPair]:
        for pair in self.pairs:
            if pair.translation_id == translation_id:
                return pair
        return None

    def source_word_totals(self) -> Counter[str]:
        total: Counter[str] = Counter()
        for pair in self.pairs:
            total.update(pair.source_multiset)
        return total

    def target_word_totals(self) -> Counter[str]:
        total: Counter[str] = Counter()
        for pair in self.pairs:
            total.update(pair.target_multiset)
        return total


def build_book(
    document_id: str,
    language: str,
    package_id: str,
    content_sha256: str,
    source_segments: Sequence[tuple[str, int, str]],
    chunks: Sequence[dict[str, object]],
) -> Textbook:
    """Build a booklet mirroring a package's delivered chunks."""

    by_id = {sid: text for sid, _rev, text in source_segments}
    pairs: list[TextbookPair] = []
    for chunk in chunks:
        source_ids = tuple(str(s) for s in chunk["source_ids"])
        missing = [sid for sid in source_ids if sid not in by_id]
        if missing:
            raise TextbookContradiction(
                "package chunk references source segment(s) absent from the "
                f"snapshot: {missing}"
            )
        source_words: Counter[str] = Counter()
        for sid in source_ids:
            source_words.update(tokenize(by_id[sid]))
        revisions = tuple(int(r) for r in chunk.get("source_revisions", ()))
        pairs.append(
            TextbookPair(
                translation_id=str(chunk["translation_id"]),
                source_ids=source_ids,
                source_revisions=revisions,
                source_words=frozen_multiset(source_words.elements()),
                target_words=frozen_multiset(tokenize(str(chunk["text"]))),
            )
        )

    source_vocab = tuple(sorted({w for pair in pairs for w, _c in pair.source_words}))
    target_vocab = tuple(sorted({w for pair in pairs for w, _c in pair.target_words}))
    return Textbook(
        document_id=document_id,
        language=language,
        package_id=package_id,
        content_sha256=content_sha256,
        source_vocab=source_vocab,
        target_vocab=target_vocab,
        pairs=tuple(pairs),
    )


def analyze_package(product: SourceProduct, package: dict[str, object]) -> Textbook:
    """Build a booklet from a real package and the *published* source snapshot.

    The package's frozen snapshot revisions are cross-checked against the
    published source, and the chunk sequence must reproduce the snapshot
    exactly once in order, so a booklet is never assembled from mismatched text.
    """

    snapshot = package.get("source_snapshot")
    if not isinstance(snapshot, list) or not snapshot:
        raise TextbookContradiction("package has no source snapshot")
    live = product.index_by_segment_id()
    source_segments: list[tuple[str, int, str]] = []
    for item in snapshot:
        sid = str(item["segment_id"])
        rev = int(item["revision"])
        if sid not in live:
            raise TextbookContradiction(
                f"package snapshot segment {sid!r} is absent from the source"
            )
        if live[sid].revision != rev:
            raise TextbookContradiction(
                f"package snapshot has {sid}@{rev} but the source publishes "
                f"{sid}@{live[sid].revision}"
            )
        source_segments.append((sid, rev, live[sid].text))

    expected = [str(item["segment_id"]) for item in snapshot]
    covered = [str(sid) for chunk in package["chunks"] for sid in chunk["source_ids"]]
    if covered != expected:
        raise TextbookContradiction(
            "package chunks do not reproduce its source snapshot exactly once "
            f"in order (covered={covered!r}, snapshot={expected!r})"
        )
    return build_book(
        document_id=str(package["document_id"]),
        language=str(package["language"]),
        package_id=str(package["package_id"]),
        content_sha256=str(package.get("content_sha256", "")),
        source_segments=source_segments,
        chunks=package["chunks"],
    )


def verify_package_against_book(book: Textbook, package: dict[str, object]) -> None:
    """Reject borrowing an old chunking just because its text reads the same.

    Ids, source coverage, revisions and both multisets must match.  Identical
    rendered text is *not* sufficient reason to reuse a differing chunking.
    """

    if str(package.get("package_id")) != book.package_id:
        raise TextbookContradiction(
            f"package id {package.get('package_id')!r} differs from booklet "
            f"package {book.package_id!r}"
        )
    if str(package.get("content_sha256", "")) != book.content_sha256:
        raise TextbookContradiction(
            "package content_sha256 does not match the booklet's frozen package"
        )
    chunks = package.get("chunks")
    snapshot = package.get("source_snapshot")
    if not isinstance(chunks, list) or not isinstance(snapshot, list):
        raise TextbookContradiction("package chunks/snapshot are missing")
    by_id = {str(item["segment_id"]): int(item["revision"]) for item in snapshot}
    if len(chunks) != len(book.pairs):
        raise TextbookContradiction(
            f"package has {len(chunks)} chunks but the booklet has "
            f"{len(book.pairs)} pairs"
        )
    for chunk, pair in zip(chunks, book.pairs):
        if str(chunk["translation_id"]) != pair.translation_id:
            raise TextbookContradiction(
                f"chunk id {chunk['translation_id']!r} differs from booklet "
                f"pair {pair.translation_id!r}"
            )
        ids = tuple(str(s) for s in chunk["source_ids"])
        if ids != pair.source_ids:
            raise TextbookContradiction(
                f"chunk {pair.translation_id!r} covers {ids!r}, booklet covers "
                f"{pair.source_ids!r}"
            )
        revisions = tuple(by_id[sid] for sid in ids)
        if revisions != pair.source_revisions:
            raise TextbookContradiction(
                f"chunk {pair.translation_id!r} revisions {revisions!r} differ "
                f"from booklet {pair.source_revisions!r}"
            )
        if frozen_multiset(tokenize(str(chunk["text"]))) != pair.target_words:
            raise TextbookContradiction(
                f"chunk {pair.translation_id!r} target words differ from booklet"
            )


# ---------------------------------------------------------------------------
# dictionary desk
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DictionaryCheck:
    """Answer to "can these ``(source, target)`` pairs share one dictionary?".

    ``feasible`` means the pairs are jointly injective, each is legal for the
    booklet, and they extend to a full document-wide bijection.  ``dictionary``
    is a complete witness when feasible; ``contradiction`` explains failure.
    """

    feasible: bool
    dictionary: Optional[dict[str, str]]
    contradiction: Optional[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "feasible": self.feasible,
            "dictionary": dict(self.dictionary) if self.dictionary else None,
            "contradiction": self.contradiction,
        }


def _perfect_matching(
    candidates: Sequence[Sequence[int]],
    locked: Optional[dict[int, int]] = None,
) -> Optional[list[int]]:
    """Kuhn's augmenting-path matching with optional pre-assigned rows.

    Returns one target index per source row, or ``None`` if no perfect matching
    exists.  ``locked`` fixes ``source row -> target index`` pairs.
    """

    n = len(candidates)
    locked = dict(locked or {})
    row_to_target = [-1] * n
    target_to_row = [-1] * n
    for row, target in locked.items():
        if not (0 <= row < n) or not (0 <= target < n):
            return None
        if row_to_target[row] != -1 or target_to_row[target] != -1:
            return None
        row_to_target[row] = target
        target_to_row[target] = row
    locked_targets = set(locked.values())

    def augment(row: int, seen: set[int]) -> bool:
        for target in candidates[row]:
            if target in locked_targets or target in seen:
                continue
            seen.add(target)
            occupant = target_to_row[target]
            if occupant == -1 or augment(occupant, seen):
                target_to_row[target] = row
                row_to_target[row] = target
                return True
        return False

    for row in range(n):
        if row_to_target[row] == -1:
            if not augment(row, set()):
                return None
    return row_to_target


class DictionarySpace:
    """All legal dictionaries of a booklet, with witnesses for pair requests."""

    def __init__(self, book: Textbook) -> None:
        self.book = book
        if len(book.source_vocab) > MAX_DICTIONARY_PAIRS:
            raise TextbookLimitExceeded(
                f"booklet has {len(book.source_vocab)} source words; the "
                f"published limit is {MAX_DICTIONARY_PAIRS} pairs"
            )
        if len(book.target_vocab) > MAX_DICTIONARY_PAIRS:
            raise TextbookLimitExceeded(
                f"booklet has {len(book.target_vocab)} target words; the "
                f"published limit is {MAX_DICTIONARY_PAIRS} pairs"
            )
        self.source_words = tuple(book.source_vocab)
        self.target_words = tuple(book.target_vocab)
        self._sidx = {w: i for i, w in enumerate(self.source_words)}
        self._tidx = {w: i for i, w in enumerate(self.target_words)}
        self.candidates = self._compute_candidates()
        self._images: Optional[dict[str, tuple[str, ...]]] = None

    # -- candidate construction -------------------------------------------
    def _compute_candidates(self) -> tuple[tuple[int, ...], ...]:
        n_s = len(self.source_words)
        n_t = len(self.target_words)
        if n_s != n_t:
            return tuple(() for _ in range(n_s))
        allowed = [set(range(n_t)) for _ in range(n_s)]
        for pair in self.book.pairs:
            s_ms = pair.source_multiset
            t_ms = pair.target_multiset
            for sw, cnt in s_ms.items():
                si = self._sidx[sw]
                permitted = {self._tidx[tw] for tw, c in t_ms.items() if c == cnt}
                allowed[si] &= permitted
        return tuple(tuple(sorted(a)) for a in allowed)

    # -- public facts ------------------------------------------------------
    @property
    def consistent(self) -> bool:
        return _perfect_matching(self.candidates) is not None

    @property
    def dictionary(self) -> Optional[dict[str, str]]:
        """One legal dictionary (lexicographic within the candidate graph)."""

        row_targets = _perfect_matching(self.candidates)
        if row_targets is None:
            return None
        return {self.source_words[i]: self.target_words[t] for i, t in enumerate(row_targets)}

    @property
    def unique(self) -> bool:
        if not self.consistent:
            return False
        return len(self.all_dictionaries(limit=2)) == 1

    def image_of(self, source_word: str) -> tuple[str, ...]:
        """Target words that some *complete* legal dictionary maps this to.

        A target is possible only if locking ``source_word -> target`` still
        admits a perfect matching; merely appearing in the local candidate set
        (or in a partial assignment) is not enough.
        """

        if source_word not in self._sidx:
            raise KeyError(source_word)
        if self._images is None:
            images: dict[str, tuple[str, ...]] = {}
            for si, sw in enumerate(self.source_words):
                possible = []
                for ti in self.candidates[si]:
                    if _perfect_matching(self.candidates, {si: ti}) is not None:
                        possible.append(self.target_words[ti])
                images[sw] = tuple(sorted(possible))
            self._images = images
        return self._images[source_word]

    @property
    def contradiction(self) -> Optional[str]:
        if self.consistent:
            return None
        if len(self.source_words) != len(self.target_words):
            return (
                f"vocabularies differ in size ({len(self.source_words)} source "
                f"vs {len(self.target_words)} target); no bijection exists"
            )
        empty = [
            self.source_words[i]
            for i, cand in enumerate(self.candidates)
            if not cand
        ]
        if empty:
            return (
                "no target word can match source word(s) "
                f"{empty!r} count for count in every chunk"
            )
        return (
            "the booklet's chunks admit no document-wide bijection: the local "
            "candidate sets cannot be matched one-to-one"
        )

    # -- enumerating / answering ------------------------------------------
    def all_dictionaries(self, limit: int = 64) -> list[dict[str, str]]:
        """Enumerate legal dictionaries (bounded; deterministic order)."""

        if _perfect_matching(self.candidates) is None:
            return []
        out: list[dict[str, str]] = []

        def rec(row: int, used: set[int], chosen: dict[str, str]) -> None:
            if len(out) >= limit:
                return
            if row == len(self.source_words):
                out.append(dict(chosen))
                return
            for ti in self.candidates[row]:
                if ti in used:
                    continue
                chosen[self.source_words[row]] = self.target_words[ti]
                used.add(ti)
                rec(row + 1, used, chosen)
                used.remove(ti)
                del chosen[self.source_words[row]]
                if len(out) >= limit:
                    return

        rec(0, set(), {})
        return out

    def check_pairs(self, requested: Iterable[tuple[str, str]]) -> DictionaryCheck:
        """Can all requested ``(source, target)`` pairs share one dictionary?

        Answered exactly: each pair must be a real possible image, source and
        target words must each be used at most once, and the bundle must still
        extend to a full document-wide bijection.  Two requests can each be
        feasible while their union is not.
        """

        pairs = [(str(s), str(t)) for s, t in requested]
        if not self.consistent:
            return DictionaryCheck(False, None, "the booklet admits no legal dictionary")
        owner_s: dict[int, int] = {}
        owner_t: dict[int, int] = {}
        for s, t in pairs:
            if s not in self._sidx:
                return DictionaryCheck(False, None, f"unknown source word {s!r}")
            if t not in self._tidx:
                return DictionaryCheck(False, None, f"unknown target word {t!r}")
            si = self._sidx[s]
            ti = self._tidx[t]
            if ti not in self.candidates[si]:
                return DictionaryCheck(
                    False, None, f"{s}->{t} is impossible under the booklet's chunks"
                )
            if si in owner_s and owner_s[si] != ti:
                return DictionaryCheck(
                    False, None, f"{s} is pinned to both "
                    f"{self.target_words[owner_s[si]]!r} and {t!r}"
                )
            if ti in owner_t and owner_t[ti] != si:
                return DictionaryCheck(
                    False, None,
                    f"target {t!r} is pinned to both "
                    f"{self.source_words[owner_t[ti]]!r} and {s!r}",
                )
            owner_s[si] = ti
            owner_t[ti] = si
        row_targets = _perfect_matching(self.candidates, owner_s)
        if row_targets is None:
            return DictionaryCheck(
                False,
                None,
                "the requested pairs are each legal but have no joint extension "
                "to a full document-wide dictionary",
            )
        dictionary = {
            self.source_words[i]: self.target_words[t]
            for i, t in enumerate(row_targets)
        }
        return DictionaryCheck(True, dictionary, None)

    def find_joint_contradiction(
        self,
    ) -> Optional[tuple[tuple[str, str], tuple[str, str]]]:
        """Witness of two pair requests feasible alone but not together."""

        if not self.consistent:
            return None
        singles = [
            (sw, tw)
            for sw in self.source_words
            for tw in self.image_of(sw)
        ]
        seen: set[tuple[tuple[str, str], tuple[str, str]]] = set()
        for first in singles:
            for second in singles:
                if first == second:
                    continue
                key = (min(first, second), max(first, second))
                if key in seen:
                    continue
                seen.add(key)
                if not self.check_pairs([first, second]).feasible:
                    return first, second
        return None


# ---------------------------------------------------------------------------
# tile board
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Exercise:
    """One practice sheet: a table, a source-word multiset, and its points."""

    exercise_id: str
    table: str
    source_words: tuple[tuple[str, int], ...]
    points: int

    def __post_init__(self) -> None:
        if self.points <= 0:
            raise ExerciseContradiction(
                f"exercise {self.exercise_id!r} has non-positive points"
            )
        if not self.source_words:
            raise ExerciseContradiction(
                f"exercise {self.exercise_id!r} has an empty word multiset"
            )

    @classmethod
    def from_tokens(
        cls, exercise_id: str, table: str, words: Iterable[str], points: int
    ) -> "Exercise":
        return cls(exercise_id, table, frozen_multiset(words), int(points))

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Exercise":
        return cls(
            exercise_id=str(data["exercise_id"]),
            table=str(data["table"]),
            source_words=tuple(
                (str(w), int(c))
                for w, c in sorted(dict(data["source_words"]).items())
            ),
            points=int(data["points"]),
        )

    @property
    def source_multiset(self) -> Counter[str]:
        return Counter(dict(self.source_words))

    def to_dict(self) -> dict[str, object]:
        return {
            "exercise_id": self.exercise_id,
            "table": self.table,
            "source_words": {w: c for w, c in self.source_words},
            "points": self.points,
        }


@dataclass(frozen=True)
class TablePlan:
    """One table's pre-packed bag and the sheet it accepts."""

    table: str
    exercise_id: str
    points: int
    bag: tuple[tuple[str, int], ...]
    tile_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "table": self.table,
            "exercise_id": self.exercise_id,
            "points": self.points,
            "bag": {w: c for w, c in self.bag},
            "tile_count": self.tile_count,
        }


@dataclass(frozen=True)
class Shortfall:
    """An unselected sheet and the tile kinds that would run short for it."""

    exercise_id: str
    table: str
    points: int
    shortages: tuple[tuple[str, int], ...]

    @property
    def primary(self) -> tuple[str, int]:
        return self.shortages[0]

    def to_dict(self) -> dict[str, object]:
        return {
            "exercise_id": self.exercise_id,
            "table": self.table,
            "points": self.points,
            "shortages": {w: c for w, c in self.shortages},
            "primary_kind": self.primary[0],
            "primary_deficit": self.primary[1],
        }


@dataclass(frozen=True)
class AllocationResult:
    ok: bool
    score: int
    plans: tuple[TablePlan, ...]
    unselected: tuple[Shortfall, ...]
    supply: tuple[tuple[str, int], ...]
    contradiction: Optional[str] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "score": self.score,
            "supply": {w: c for w, c in self.supply},
            "plans": [p.to_dict() for p in self.plans],
            "table_tile_counts": {p.table: p.tile_count for p in self.plans},
            "unselected": [u.to_dict() for u in self.unselected],
            "contradiction": self.contradiction,
        }


def package_supply(book: Textbook) -> dict[str, int]:
    """Target-word copies the retained bundle(s) put in the store.

    The published package supplies exactly its target-word totals; every table
    draws its bag from this shared pool.
    """

    return {w: c for w, c in book.target_word_totals().items()}


def _add(dest: dict[str, int], src: dict[str, int], sign: int = 1) -> None:
    for k, v in src.items():
        dest[k] = dest.get(k, 0) + sign * v


class TileAllocationProblem:
    """Choose sheets to serve, one per table, against a shared tile supply.

    Every sheet is for its own table, so at most one is accepted per table.
    A tile kind is short when the total demand of all served sheets exceeds the
    copies in the store.  Tiles of one kind are interchangeable, so a per-kind
    total-demand bound is exactly the packing condition (a physical tile is
    never packed into two bags).
    """

    def __init__(
        self,
        book: Textbook,
        space: DictionarySpace,
        exercises: Sequence[Exercise],
        supply: dict[str, int],
    ) -> None:
        if len(exercises) > MAX_EXERCISES:
            raise TextbookLimitExceeded(
                f"{len(exercises)} exercises; the published limit is {MAX_EXERCISES}"
            )
        self.book = book
        self.space = space
        if not space.consistent:
            raise TextbookContradiction(
                "tiles cannot be packed for a contradictory booklet"
            )
        self.exercises = tuple(exercises)
        ids = [ex.exercise_id for ex in exercises]
        tables = [ex.table for ex in exercises]
        if len(set(ids)) != len(ids):
            raise ExerciseContradiction(f"duplicate exercise ids: {ids!r}")
        if len(set(tables)) != len(tables):
            raise ExerciseContradiction(
                f"two sheets share a table: {tables!r}; each table accepts one"
            )
        self.supply = {str(k): int(v) for k, v in supply.items()}
        if any(v < 0 for v in self.supply.values()):
            raise ExerciseContradiction("tile supply cannot be negative")

    def required_tiles(self, exercise: Exercise) -> dict[str, int]:
        """Copies per target kind needed under *every* compatible dictionary.

        Under a legal dictionary a target kind is claimed by exactly one source
        word (the dictionary is a bijection), so kind ``k`` spells
        ``exercise[w]`` where ``w`` is the unique source word mapped to ``k``.
        To work under *all* dictionaries at once the bag must hold, for each
        ``k``, the largest count any compatible preimage could demand:
        ``max{ exercise[w] : k in image(w) }``.  Summing images instead would
        double-count a single source word that has several possible images.
        """

        required: dict[str, int] = {}
        for sw, cnt in exercise.source_words:
            if sw not in self.space._sidx:
                raise ExerciseContradiction(
                    f"exercise {exercise.exercise_id!r} uses source word {sw!r} "
                    "absent from the booklet vocabulary"
                )
            images = self.space.image_of(sw)
            if not images:
                raise ExerciseContradiction(
                    f"source word {sw!r} has no possible target image"
                )
            for tw in images:
                required[tw] = max(required.get(tw, 0), cnt)
        return required

    # -- optimisation ------------------------------------------------------
    def solve(self) -> AllocationResult:
        """Maximise total points by selecting at most one sheet per table.

        All selected sheets draw from the one shared supply; a kind is feasible
        only while cumulative demand stays within the copies in the store.  A
        depth-first branch-and-bound enumerates subsets (<=22 sheets) with a
        points upper bound.  Ties on score are broken deterministically by
        fewer tiles, then fewer kinds, then lexicographic exercise ids so a
        repeat run always returns the same allocation.
        """

        required = [self.required_tiles(ex) for ex in self.exercises]
        n = len(self.exercises)
        points = [ex.points for ex in self.exercises]

        # Upper bound on remaining points from index i onward.
        suffix_points = [0] * (n + 1)
        for i in range(n - 1, -1, -1):
            suffix_points[i] = suffix_points[i + 1] + points[i]

        best: dict[str, object] = {
            "score": 0,
            "tiles": 0,
            "kinds": 0,
            "ids": (),
            "chosen": (),
            "found": False,
        }

        def better(score: int, tiles: int, kinds: int, ids: tuple[str, ...]) -> bool:
            if not best["found"]:
                return True
            current = (int(best["score"]), -int(best["tiles"]), -int(best["kinds"]))
            candidate = (score, -tiles, -kinds)
            if candidate != current:
                return candidate > current
            return ids < tuple(best["ids"])  # type: ignore[arg-type]

        used: dict[str, int] = {}
        used_tables: set[str] = set()

        def dfs(i: int, score: int, chosen: list[int]) -> None:
            if score + suffix_points[i] < int(best["score"]):
                return
            if i == n:
                ids = tuple(sorted(self.exercises[j].exercise_id for j in chosen))
                demand_total: dict[str, int] = {}
                for j in chosen:
                    _add(demand_total, required[j])
                tiles = sum(demand_total.values())
                kinds = len(demand_total)
                if better(score, tiles, kinds, ids):
                    best.update(
                        score=score,
                        tiles=tiles,
                        kinds=kinds,
                        ids=ids,
                        chosen=tuple(chosen),
                        found=True,
                    )
                return
            # skip this sheet
            dfs(i + 1, score, chosen)
            # take it, if its table is free and the shared supply can spare it
            ex = self.exercises[i]
            demand = required[i]
            if ex.table in used_tables:
                return
            if any(used.get(k, 0) + v > self.supply.get(k, 0) for k, v in demand.items()):
                return
            used_tables.add(ex.table)
            _add(used, demand)
            chosen.append(i)
            dfs(i + 1, score + points[i], chosen)
            chosen.pop()
            _add(used, demand, -1)
            used_tables.discard(ex.table)

        dfs(0, 0, [])

        chosen = list(best["chosen"])  # type: ignore[arg-type]
        plans: list[TablePlan] = []
        for idx in sorted(chosen):
            ex = self.exercises[idx]
            bag = required[idx]
            plans.append(
                TablePlan(
                    table=ex.table,
                    exercise_id=ex.exercise_id,
                    points=ex.points,
                    bag=tuple(sorted(bag.items())),
                    tile_count=sum(bag.values()),
                )
            )

        # For every unselected sheet: keeping all selected sheets, how many
        # extra copies of each kind would be needed to also serve it.
        used_total: dict[str, int] = {}
        for idx in chosen:
            _add(used_total, required[idx])
        selected_ids = {self.exercises[i].exercise_id for i in chosen}
        unselected: list[Shortfall] = []
        for idx, ex in enumerate(self.exercises):
            if ex.exercise_id in selected_ids:
                continue
            deficits: dict[str, int] = {}
            for k, v in required[idx].items():
                over = used_total.get(k, 0) + v - self.supply.get(k, 0)
                if over > 0:
                    deficits[k] = over
            if not deficits:
                raise TextbookError(
                    f"unselected exercise {ex.exercise_id!r} already fits the "
                    "remaining tiles; the search should have selected it"
                )
            ordered = tuple(
                sorted(deficits.items(), key=lambda kv: (-kv[1], kv[0]))
            )
            unselected.append(
                Shortfall(
                    exercise_id=ex.exercise_id,
                    table=ex.table,
                    points=ex.points,
                    shortages=ordered,
                )
            )

        return AllocationResult(
            ok=True,
            score=int(best["score"]),
            plans=tuple(plans),
            unselected=tuple(unselected),
            supply=tuple(sorted(self.supply.items())),
            contradiction=None,
        )


def solve_allocation(
    book: Textbook,
    space: DictionarySpace,
    exercises: Sequence[Exercise],
    supply: Optional[dict[str, int]] = None,
) -> AllocationResult:
    """Convenience wrapper; defaults to the retained bundle's supply."""

    problem = TileAllocationProblem(
        book, space, exercises, supply if supply is not None else package_supply(book)
    )
    return problem.solve()
