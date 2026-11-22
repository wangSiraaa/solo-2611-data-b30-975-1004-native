"""Immutable value objects for the rights desk and the distribution desk.

Two desks sit on top of the frozen translation packages:

* **Rights desk** — original rights holders register usage rights against the
  *actual* translation blocks (strip id + every source revision), then delegate
  them down chains.  Every link narrows the scope by intersection
  (venue / language / inclusive date window / purpose), and a link created by a
  non-sublicensable grantor is void.  The same right may reach one signer over
  several independent paths; revoking one delegation only cuts paths through it.
* **Distribution desk** — buys session tickets.  A ticket only counts for the
  rights the vendor can actually sign for the actual block on that show day, and
  at most one ticket per mutually exclusive quote group may be bought.

Block identity is the strip (translation) id plus the *full* set of
``(segment_id, revision)`` references.  A package chunk with the same text but a
different strip id is a different block and can never borrow another block's
registration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import SourceRef


def _sorted_refs(refs: tuple[SourceRef, ...]) -> tuple[SourceRef, ...]:
    return tuple(sorted(refs, key=lambda r: (r.segment_id, r.revision)))


# Sentinel used while intersecting incompatible concrete values.
_CONFLICT = object()


@dataclass(frozen=True)
class Scope:
    """A delegable scope; ``None`` on a dimension means "unrestricted".

    Dates are inclusive ISO ``YYYY-MM-DD`` strings and compare lexicographically.
    """

    venue: Optional[str] = None
    language: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    purpose: Optional[str] = None

    def __post_init__(self) -> None:
        # A reversed window is not a construction error: it is an empty scope,
        # reported through ``is_empty`` so intersections can produce it.
        return None

    @staticmethod
    def intersect(left: "Scope", right: "Scope") -> "Scope":
        """Intersection of two scopes: every dimension takes the tighter bound."""

        def intersect_equal_dimension(
            a: Optional[str], b: Optional[str]
        ) -> Optional[str]:
            # A wildcard yields the other side; two concrete values must match,
            # otherwise the dimension is an empty (impossible) intersection.
            if a is None:
                return b
            if b is None:
                return a
            return a if a == b else _CONFLICT  # type: ignore[return-value]

        date_from = (
            left.date_from
            if right.date_from is None
            else right.date_from
            if left.date_from is None
            else max(left.date_from, right.date_from)
        )
        date_to = (
            left.date_to
            if right.date_to is None
            else right.date_to
            if left.date_to is None
            else min(left.date_to, right.date_to)
        )
        return Scope(
            venue=intersect_equal_dimension(left.venue, right.venue),
            language=intersect_equal_dimension(left.language, right.language),
            date_from=date_from,
            date_to=date_to,
            purpose=intersect_equal_dimension(left.purpose, right.purpose),
        )

    @property
    def is_empty(self) -> bool:
        """An empty intersection can never authorize any concrete screening."""

        if _CONFLICT in (self.venue, self.language, self.purpose):
            return True
        if self.date_from is not None and self.date_to is not None:
            return self.date_from > self.date_to
        return False

    def contains(
        self,
        *,
        venue: str,
        language: str,
        day: str,
        purpose: Optional[str] = None,
    ) -> bool:
        if self.venue is not None and self.venue != venue:
            return False
        if self.language is not None and self.language != language:
            return False
        if self.date_from is not None and day < self.date_from:
            return False
        if self.date_to is not None and day > self.date_to:
            return False
        if self.purpose is not None and self.purpose != purpose:
            return False
        return True

    def to_dict(self) -> dict[str, Optional[str]]:
        return {
            "venue": self.venue,
            "language": self.language,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "purpose": self.purpose,
        }


@dataclass(frozen=True)
class BlockRef:
    """Identity of an actual translation block.

    The registration is bound to the strip id and to *every* source revision the
    block renders — never to the text alone.
    """

    strip_id: str
    refs: tuple[SourceRef, ...]

    def __post_init__(self) -> None:
        if not self.strip_id:
            raise ValueError("strip_id must be non-empty")
        if not self.refs:
            raise ValueError("a block must reference at least one source segment")
        object.__setattr__(self, "refs", _sorted_refs(self.refs))

    @staticmethod
    def from_chunk(chunk: dict[str, object]) -> "BlockRef":
        # translation-package/1 chunks keep ordered source id strings plus a
        # parallel list of the exact revisions rendered by this strip.
        ids = [str(x) for x in chunk["source_ids"]]  # type: ignore[index]
        revisions = chunk.get("source_revisions")
        if revisions is None:
            # Defensive: some hand-written products only carry {id, revision}
            # dicts inside source_ids.
            refs = tuple(
                SourceRef(segment_id=str(item["segment_id"]), revision=int(item["revision"]))
                for item in chunk["source_ids"]  # type: ignore[index]
            )
        else:
            refs = tuple(
                SourceRef(segment_id=sid, revision=int(rev))
                for sid, rev in zip(ids, revisions)
            )
        return BlockRef(strip_id=str(chunk["translation_id"]), refs=refs)

    @property
    def ref_set(self) -> frozenset[tuple[str, int]]:
        return frozenset((r.segment_id, r.revision) for r in self.refs)

    def matches(self, other: "BlockRef") -> bool:
        return self.strip_id == other.strip_id and self.ref_set == other.ref_set

    def to_dict(self) -> dict[str, object]:
        return {
            "strip_id": self.strip_id,
            "refs": [
                {"segment_id": r.segment_id, "revision": r.revision} for r in self.refs
            ],
        }


@dataclass(frozen=True)
class UsageRegistration:
    """One usage registration for an actual block; all listed rights are required."""

    registration_id: str
    document_id: str
    block: BlockRef
    rights: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.registration_id:
            raise ValueError("registration_id must be non-empty")
        if not self.rights:
            raise ValueError("a registration must list at least one usage right")
        if len(set(self.rights)) != len(self.rights):
            raise ValueError(f"duplicate rights in registration {self.registration_id!r}")
        object.__setattr__(self, "rights", tuple(self.rights))


@dataclass(frozen=True)
class RootGrant:
    """An original usage right held by a rights holder for one registration."""

    grant_id: str
    document_id: str
    registration_id: str
    right: str
    rights_holder: str
    scope: Scope = field(default_factory=Scope)
    sublicensable: bool = True


@dataclass(frozen=True)
class Delegation:
    """One delegation link naming the previous grant and the next trustee.

    The declared scope is intersected with every ancestor scope along the chain.
    """

    delegation_id: str
    document_id: str
    parent_id: str  # root grant id or another delegation id
    trustee: str
    scope: Scope
    sublicensable: bool = True
    revoked: bool = False


@dataclass(frozen=True)
class ChainLink:
    """One hop of a reconstructed effective chain."""

    kind: str  # "root" or "delegation"
    id: str
    party: str  # rights holder for a root, trustee for a delegation
    declared_scope: Scope
    sublicensable: bool
    revoked: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "id": self.id,
            "party": self.party,
            "declared_scope": self.declared_scope.to_dict(),
            "sublicensable": self.sublicensable,
            "revoked": self.revoked,
        }


@dataclass(frozen=True)
class ChainPath:
    """A valid chain from the original rights holder to a signing vendor."""

    right: str
    registration_id: str
    root_grant_id: str
    holder: str
    signer: str
    effective_scope: Scope
    links: tuple[ChainLink, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "right": self.right,
            "registration_id": self.registration_id,
            "root_grant_id": self.root_grant_id,
            "rights_holder": self.holder,
            "signer": self.signer,
            "effective_scope": self.effective_scope.to_dict(),
            "links": [link.to_dict() for link in self.links],
        }


@dataclass(frozen=True)
class SigningAnswer:
    """The rights desk's standalone answer for one vendor/block/right question."""

    can_sign: bool
    vendor: str
    block: BlockRef
    right: str
    venue: str
    language: str
    day: str
    purpose: Optional[str]
    chains: tuple[ChainPath, ...]
    other_sources: tuple[ChainPath, ...]  # valid chains reaching other issuers

    def to_dict(self) -> dict[str, object]:
        return {
            "can_sign": self.can_sign,
            "vendor": self.vendor,
            "block": self.block.to_dict(),
            "right": self.right,
            "venue": self.venue,
            "language": self.language,
            "day": self.day,
            "purpose": self.purpose,
            "chains": [c.to_dict() for c in self.chains],
            "other_sources": [c.to_dict() for c in self.other_sources],
        }


@dataclass(frozen=True)
class Ticket:
    """One session ticket offered by the distribution desk.

    ``group`` is the mutually exclusive quote group: at most one ticket per group
    may be bought.  Tickets without a group never conflict with each other.
    """

    ticket_id: str
    vendor: str
    rights: tuple[str, ...]
    days: tuple[str, ...]
    fee: float
    group: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.ticket_id:
            raise ValueError("ticket_id must be non-empty")
        if not self.rights:
            raise ValueError("a ticket must name at least one right")
        if not self.days:
            raise ValueError("a ticket must apply to at least one day")
        object.__setattr__(self, "rights", tuple(self.rights))
        object.__setattr__(self, "days", tuple(sorted(self.days)))
        if self.fee < 0:
            raise ValueError("ticket fee must be non-negative")


@dataclass(frozen=True)
class RequirementAtom:
    """One (block, day, right) cell that must be covered by a bought ticket."""

    index: int
    block: BlockRef
    day: str
    right: str

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "block": self.block.to_dict(),
            "day": self.day,
            "right": self.right,
        }


@dataclass(frozen=True)
class Credential:
    """The effective signing evidence for one requirement atom."""

    atom: RequirementAtom
    ticket_id: str
    vendor: str
    chain: ChainPath

    def to_dict(self) -> dict[str, object]:
        return {
            "requirement": self.atom.to_dict(),
            "ticket_id": self.ticket_id,
            "vendor": self.vendor,
            "chain": self.chain.to_dict(),
        }


@dataclass(frozen=True)
class TicketPlan:
    """The minimum-fee purchase plan, or an explicit refusal."""

    ok: bool
    venue: str
    language: str
    days: tuple[str, ...]
    selected_ticket_ids: tuple[str, ...]
    total_fee: Optional[float]
    requirements: tuple[RequirementAtom, ...]
    credentials: tuple[Credential, ...]
    reason: Optional[str] = None
    uncovered: tuple[RequirementAtom, ...] = ()
    unregistered_blocks: tuple[BlockRef, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "venue": self.venue,
            "language": self.language,
            "days": list(self.days),
            "selected_ticket_ids": list(self.selected_ticket_ids),
            "total_fee": self.total_fee,
            "requirements": [a.to_dict() for a in self.requirements],
            "credentials": [c.to_dict() for c in self.credentials],
            "reason": self.reason,
            "uncovered": [a.to_dict() for a in self.uncovered],
            "unregistered_blocks": [b.to_dict() for b in self.unregistered_blocks],
        }
