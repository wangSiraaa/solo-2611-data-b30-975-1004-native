"""Pure rights-chain reasoning and minimum-fee ticket planning.

The engine is deliberately persistence-free: a :class:`RightsRepository` hands
it immutable registrations, root grants and delegations, and all answers are
recomputed from that snapshot.  Revocation therefore needs no cache
invalidation — a revoked delegation is simply skipped when paths are rebuilt.

Chain rules implemented here:

* scope (venue / language / inclusive date window / purpose) is intersected at
  every hop, and a hop with an empty intersection is not a usable path;
* a delegation whose grantor may not sublicense is void, together with every
  link beneath it;
* a revoked delegation cuts only the paths passing through it;
* the same right may reach one or more signers over independent paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from .errors import RightsDataError, UnknownAuthorization
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


@dataclass(frozen=True)
class _Frame:
    """Internal DFS frame: where we are and the scope accumulated so far."""

    parent_id: str
    scope: Scope
    links: tuple[ChainLink, ...]
    signer: Optional[str]


class RightsEngine:
    def __init__(
        self,
        registrations: Iterable[UsageRegistration],
        root_grants: Iterable[RootGrant],
        delegations: Iterable[Delegation],
        legacy_document_ids: Iterable[str] = (),
    ) -> None:
        self.registrations: dict[str, UsageRegistration] = {
            r.registration_id: r for r in registrations
        }
        self.root_grants: dict[str, RootGrant] = {g.grant_id: g for g in root_grants}
        self.delegations: dict[str, Delegation] = {d.delegation_id: d for d in delegations}
        self.legacy_document_ids = frozenset(legacy_document_ids)
        self._validate_references()
        self._rebuild_indexes()

    def _rebuild_indexes(self) -> None:
        """Rebuild parent -> children adjacency after an in-memory test edit."""

        self._children: dict[str, list[Delegation]] = {}
        for delegation in self.delegations.values():
            self._children.setdefault(delegation.parent_id, []).append(delegation)
        for children in self._children.values():
            children.sort(key=lambda d: d.delegation_id)

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------
    def _validate_references(self) -> None:
        known = set(self.root_grants) | set(self.delegations)
        for grant in self.root_grants.values():
            if grant.registration_id not in self.registrations:
                raise RightsDataError(
                    f"root grant {grant.grant_id!r} references unknown registration"
                    f" {grant.registration_id!r}"
                )
            reg = self.registrations[grant.registration_id]
            if reg.document_id != grant.document_id:
                raise RightsDataError(
                    f"root grant {grant.grant_id!r} document does not match its registration"
                )
            if grant.right not in reg.rights:
                raise RightsDataError(
                    f"root grant {grant.grant_id!r} right {grant.right!r} is not listed by"
                    f" registration {grant.registration_id!r}"
                )
            if grant.scope.is_empty:
                raise RightsDataError(f"root grant {grant.grant_id!r} has an empty scope")
        for delegation in self.delegations.values():
            if delegation.parent_id not in known:
                raise RightsDataError(
                    f"delegation {delegation.delegation_id!r} names unknown parent"
                    f" {delegation.parent_id!r}"
                )
            parent = self._parent_of(delegation.parent_id)
            if parent.document_id != delegation.document_id:
                raise RightsDataError(
                    f"delegation {delegation.delegation_id!r} crosses document boundaries"
                )
            if delegation.scope.is_empty:
                raise RightsDataError(
                    f"delegation {delegation.delegation_id!r} has an empty declared scope"
                )

    def _parent_of(self, parent_id: str) -> RootGrant | Delegation:
        if parent_id in self.root_grants:
            return self.root_grants[parent_id]
        if parent_id in self.delegations:
            return self.delegations[parent_id]
        raise UnknownAuthorization(f"parent authorization {parent_id!r} not found")

    # ------------------------------------------------------------------
    # block / registration lookup
    # ------------------------------------------------------------------
    def find_registrations(self, document_id: str, block: BlockRef) -> list[UsageRegistration]:
        """All usage registrations bound to one actual block.

        A block may carry several independent registrations (each rights holder
        lists one or more original usage rights), so the lookup is plural.
        """

        return [
            reg
            for reg in self.registrations.values()
            if reg.document_id == document_id and reg.block.matches(block)
        ]

    def find_registration(self, document_id: str, block: BlockRef) -> Optional[UsageRegistration]:
        """The first matching registration; convenience for single-registration books."""

        matches = self.find_registrations(document_id, block)
        return matches[0] if matches else None

    def is_legacy(self, document_id: str) -> bool:
        """Old books without any rights data stay readable and need no clearance."""

        if document_id in self.legacy_document_ids:
            return True
        return not any(r.document_id == document_id for r in self.registrations.values())

    # ------------------------------------------------------------------
    # chain traversal
    # ------------------------------------------------------------------
    def _walk_from_root(self, root: RootGrant) -> list[ChainPath]:
        """All valid paths rooted at one original grant (context unfiltered)."""

        paths: list[ChainPath] = []
        root_link = ChainLink(
            kind="root",
            id=root.grant_id,
            party=root.rights_holder,
            declared_scope=root.scope,
            sublicensable=root.sublicensable,
        )

        def visit(frame: _Frame, grantor_can_sublicense: bool, seen: set[str]) -> None:
            # Record the path reaching the current signer (holder at the root,
            # trustee after at least one delegation).
            if frame.scope.is_empty:
                return
            paths.append(
                ChainPath(
                    right=root.right,
                    registration_id=root.registration_id,
                    root_grant_id=root.grant_id,
                    holder=root.rights_holder,
                    signer=frame.signer or root.rights_holder,
                    effective_scope=frame.scope,
                    links=frame.links,
                )
            )
            if not grantor_can_sublicense:
                # Any further delegation beneath a non-sublicensable grantor is
                # void, so do not even descend.
                return
            for child in self._children.get(frame.parent_id, ()):
                if child.revoked or child.delegation_id in seen:
                    continue
                narrowed = Scope.intersect(frame.scope, child.scope)
                if narrowed.is_empty:
                    continue
                child_link = ChainLink(
                    kind="delegation",
                    id=child.delegation_id,
                    party=child.trustee,
                    declared_scope=child.scope,
                    sublicensable=child.sublicensable,
                    revoked=False,
                )
                seen.add(child.delegation_id)
                visit(
                    _Frame(
                        parent_id=child.delegation_id,
                        scope=narrowed,
                        links=frame.links + (child_link,),
                        signer=child.trustee,
                    ),
                    grantor_can_sublicense=child.sublicensable,
                    seen=seen,
                )
                seen.discard(child.delegation_id)

        visit(
            _Frame(root.grant_id, root.scope, (root_link,), None),
            root.sublicensable,
            {root.grant_id},
        )
        return self._dedupe(paths)

    @staticmethod
    def _dedupe(paths: list[ChainPath]) -> list[ChainPath]:
        seen: set[tuple[str, ...]] = set()
        unique: list[ChainPath] = []
        for path in paths:
            key = tuple(link.id for link in path.links)
            if key not in seen:
                seen.add(key)
                unique.append(path)
        unique.sort(key=lambda p: tuple(link.id for link in p.links))
        return unique

    def chains_for(
        self,
        document_id: str,
        block: BlockRef,
        right: str,
        *,
        venue: str,
        language: str,
        day: str,
        purpose: Optional[str] = None,
    ) -> list[ChainPath]:
        """Every currently effective chain for a block/right in one context."""

        regs = self.find_registrations(document_id, block)
        reg_ids = {reg.registration_id for reg in regs if right in reg.rights}
        if not reg_ids:
            return []
        result: list[ChainPath] = []
        for root in self.root_grants.values():
            if root.registration_id not in reg_ids or root.right != right:
                continue
            for path in self._walk_from_root(root):
                if path.effective_scope.contains(
                    venue=venue, language=language, day=day, purpose=purpose
                ):
                    result.append(path)
        return self._dedupe(result)

    def can_sign(
        self,
        vendor: str,
        document_id: str,
        block: BlockRef,
        right: str,
        *,
        venue: str,
        language: str,
        day: str,
        purpose: Optional[str] = None,
    ) -> SigningAnswer:
        """Standalone rights-desk answer, including other usable sources."""

        chains = self.chains_for(
            document_id,
            block,
            right,
            venue=venue,
            language=language,
            day=day,
            purpose=purpose,
        )
        own = tuple(path for path in chains if path.signer == vendor)
        other = tuple(path for path in chains if path.signer != vendor)
        return SigningAnswer(
            can_sign=bool(own),
            vendor=vendor,
            block=block,
            right=right,
            venue=venue,
            language=language,
            day=day,
            purpose=purpose,
            chains=own,
            other_sources=other,
        )

    def explain_delegation(self, delegation_id: str) -> Optional[str]:
        """Why a delegation currently yields no valid path; ``None`` if it does."""

        delegation = self.delegations.get(delegation_id)
        if delegation is None:
            raise UnknownAuthorization(f"delegation {delegation_id!r} not found")
        if delegation.revoked:
            return "revoked"
        if delegation.scope.is_empty:
            return "declared scope is empty"

        # Walk parent -> ... -> root, checking grantor sublicense flags and
        # accumulating the intersected scope handed down to this delegation.
        scope = Scope()
        node: RootGrant | Delegation = self._parent_of(delegation.parent_id)
        while True:
            if not node.sublicensable:
                label = (
                    f"root grant {node.grant_id!r}"
                    if isinstance(node, RootGrant)
                    else f"grantor delegation {node.delegation_id!r}"
                )
                return f"{label} is not sublicensable"
            if isinstance(node, Delegation) and node.revoked:
                return f"ancestor delegation {node.delegation_id!r} is revoked"
            scope = Scope.intersect(scope, node.scope)
            if isinstance(node, RootGrant):
                break
            node = self._parent_of(node.parent_id)
        if Scope.intersect(scope, delegation.scope).is_empty:
            return "effective scope after intersection is empty"
        return None

    # ------------------------------------------------------------------
    # ticket planning
    # ------------------------------------------------------------------
    def plan_tickets(
        self,
        document_id: str,
        blocks: Sequence[BlockRef],
        *,
        venue: str,
        language: str,
        days: Sequence[str],
        tickets: Sequence[Ticket],
        purpose: Optional[str] = None,
    ) -> TicketPlan:
        ordered_days = tuple(sorted(days))

        # Legacy books with no rights data remain readable: no clearance cells.
        if self.is_legacy(document_id):
            return TicketPlan(
                ok=True,
                venue=venue,
                language=language,
                days=ordered_days,
                selected_ticket_ids=(),
                total_fee=0,
                requirements=(),
                credentials=(),
            )

        unregistered = tuple(
            block
            for block in blocks
            if not self.find_registrations(document_id, block)
        )
        if unregistered:
            # A same-text/different-strip package must not borrow a registration.
            return TicketPlan(
                ok=False,
                venue=venue,
                language=language,
                days=ordered_days,
                selected_ticket_ids=(),
                total_fee=None,
                requirements=(),
                credentials=(),
                reason="unregistered_blocks",
                unregistered_blocks=unregistered,
            )

        requirements = self._build_requirements(document_id, blocks, ordered_days)
        full_mask = (1 << len(requirements)) - 1

        coverage: dict[str, int] = {}
        for ticket in tickets:
            coverage[ticket.ticket_id] = self._ticket_mask(
                ticket, document_id, requirements, venue, language, purpose
            )

        # Refusal: cells no offered ticket can ever sign. A refused session must
        # never be reported as licensed.
        coverable = 0
        for mask in coverage.values():
            coverable |= mask
        uncovered_mask = full_mask & ~coverable
        if uncovered_mask:
            uncovered = tuple(
                atom for atom in requirements if uncovered_mask & (1 << atom.index)
            )
            return TicketPlan(
                ok=False,
                venue=venue,
                language=language,
                days=ordered_days,
                selected_ticket_ids=(),
                total_fee=None,
                requirements=tuple(requirements),
                credentials=(),
                reason="uncovered_requirements",
                uncovered=uncovered,
            )

        chosen = self._minimum_fee_set_cover(tickets, coverage, full_mask)
        if chosen is None:  # pragma: no cover - coverage check above guarantees feasibility
            return TicketPlan(
                ok=False,
                venue=venue,
                language=language,
                days=ordered_days,
                selected_ticket_ids=(),
                total_fee=None,
                requirements=tuple(requirements),
                credentials=(),
                reason="mutex_group_blocks_cover",
            )

        selected = tuple(t for t in tickets if t.ticket_id in chosen)
        credentials = self._build_credentials(
            selected, document_id, requirements, venue, language, purpose
        )
        total_fee = sum(ticket.fee for ticket in selected)
        return TicketPlan(
            ok=True,
            venue=venue,
            language=language,
            days=ordered_days,
            selected_ticket_ids=tuple(sorted(t.ticket_id for t in selected)),
            total_fee=total_fee,
            requirements=tuple(requirements),
            credentials=tuple(credentials),
        )

    def _build_requirements(
        self, document_id: str, blocks: Sequence[BlockRef], days: Sequence[str]
    ) -> list[RequirementAtom]:
        atoms: list[RequirementAtom] = []
        index = 0
        for day in days:
            for block in blocks:
                regs = self.find_registrations(document_id, block)
                assert regs  # unregistered blocks handled by caller
                # Every right listed by any registration of this block must be
                # covered; the union is sorted for deterministic cell ordering.
                rights = sorted({right for reg in regs for right in reg.rights})
                for right in rights:
                    atoms.append(RequirementAtom(index=index, block=block, day=day, right=right))
                    index += 1
        return atoms

    def _ticket_mask(
        self,
        ticket: Ticket,
        document_id: str,
        requirements: Sequence[RequirementAtom],
        venue: str,
        language: str,
        purpose: Optional[str],
    ) -> int:
        mask = 0
        ticket_days = set(ticket.days)
        ticket_rights = set(ticket.rights)
        for atom in requirements:
            if atom.day not in ticket_days or atom.right not in ticket_rights:
                continue
            chains = self.chains_for(
                document_id,
                atom.block,
                atom.right,
                venue=venue,
                language=language,
                day=atom.day,
                purpose=purpose,
            )
            if any(path.signer == ticket.vendor for path in chains):
                mask |= 1 << atom.index
        return mask

    @staticmethod
    def _minimum_fee_set_cover(
        tickets: Sequence[Ticket], coverage: dict[str, int], full_mask: int
    ) -> Optional[list[str]]:
        """Exact branch-and-bound minimum-fee cover under mutex quote groups."""

        usable = [t for t in tickets if coverage[t.ticket_id]]
        # Cheapest, then id: deterministic and finds good bounds early.
        usable.sort(key=lambda t: (t.fee, t.ticket_id))

        best_ids: Optional[list[str]] = None
        best_fee: float = float("inf")
        # mask -> cheapest fee that already reached it; a costlier arrival on
        # the same mask can never improve the optimum.
        best_fee_for_mask: dict[int, float] = {}
        # mask -> minimal set of used mutex groups seen there; a state whose
        # used groups are a superset is dominated (it has fewer choices left).
        min_groups_for_mask: dict[int, frozenset[str]] = {}

        def lower_bound(mask: int, remaining: list[Ticket]) -> float:
            # Admissible: for each uncovered cell, the cheapest fee per newly
            # covered cell; summing independent per-cell minima cannot overstate
            # the cheapest way to finish.
            uncovered_bits = full_mask & ~mask
            if not uncovered_bits:
                return 0.0
            bound = 0.0
            for bit in range(uncovered_bits.bit_length()):
                if not (uncovered_bits & (1 << bit)):
                    continue
                cheapest = float("inf")
                for ticket in remaining:
                    m = coverage[ticket.ticket_id]
                    if m & (1 << bit):
                        gain = (m & uncovered_bits).bit_count()
                        cheapest = min(cheapest, ticket.fee / gain)
                bound += cheapest
            return bound

        def dfs(
            mask: int,
            fee: float,
            remaining: list[Ticket],
            chosen: list[str],
            used_groups: frozenset[str],
        ) -> None:
            nonlocal best_ids, best_fee
            if mask == full_mask:
                if fee < best_fee or (
                    fee == best_fee and (best_ids is None or chosen < best_ids)
                ):
                    best_fee = fee
                    best_ids = list(chosen)
                return
            prior_fee = best_fee_for_mask.get(mask)
            if prior_fee is not None and prior_fee <= fee:
                return
            best_fee_for_mask[mask] = fee
            prior_groups = min_groups_for_mask.get(mask)
            if prior_groups is not None and prior_groups <= used_groups:
                # A previous visit used a subset of these groups at the same or
                # lower fee: every option open here was open there too.
                return
            min_groups_for_mask[mask] = used_groups
            if fee >= best_fee:
                return
            candidates = [
                t
                for t in remaining
                if coverage[t.ticket_id] & ~mask and t.group not in used_groups
            ]
            if not candidates:
                return
            if fee + lower_bound(mask, candidates) >= best_fee:
                return
            for ticket in candidates:
                new_mask = mask | coverage[ticket.ticket_id]
                if new_mask == mask:
                    continue
                next_remaining = [t for t in remaining if t.ticket_id != ticket.ticket_id]
                next_groups = (
                    used_groups | {ticket.group} if ticket.group is not None else used_groups
                )
                chosen.append(ticket.ticket_id)
                dfs(new_mask, fee + ticket.fee, next_remaining, chosen, next_groups)
                chosen.pop()

        dfs(0, 0.0, usable, [], frozenset())
        return best_ids

    def _build_credentials(
        self,
        selected: Sequence[Ticket],
        document_id: str,
        requirements: Sequence[RequirementAtom],
        venue: str,
        language: str,
        purpose: Optional[str],
    ) -> list[Credential]:
        credentials: list[Credential] = []
        for atom in requirements:
            for ticket in selected:
                if atom.day not in ticket.days or atom.right not in ticket.rights:
                    continue
                chains = self.chains_for(
                    document_id,
                    atom.block,
                    atom.right,
                    venue=venue,
                    language=language,
                    day=atom.day,
                    purpose=purpose,
                )
                chain = next((c for c in chains if c.signer == ticket.vendor), None)
                if chain is not None:
                    credentials.append(
                        Credential(atom=atom, ticket_id=ticket.ticket_id, vendor=ticket.vendor, chain=chain)
                    )
                    break
            else:  # pragma: no cover - the mask guarantees a covering ticket
                raise RightsDataError(
                    f"no selected ticket signs {atom.right!r} for"
                    f" {atom.block.strip_id!r} on {atom.day}"
                )
        return credentials
