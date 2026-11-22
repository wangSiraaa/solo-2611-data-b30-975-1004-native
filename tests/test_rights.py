"""Tests for the rights desk (delegation chains) and the distribution desk.

Covers the exact failure modes the brief warns about:

* scope intersection at every hop and void delegations under a
  non-sublicensable grantor;
* revocation cutting only the paths through the revoked delegation while
  independent paths to the same right survive;
* tickets counting only the rights the vendor can really sign, whole-ticket
  pricing regardless of block count, and at-most-one-per mutex quote group;
* a same-text/different-strip block being unable to borrow a registration;
* legacy books without rights data remaining readable.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from delivery_system.clearance import blocks_from_package, plan_screening, screen_block
from delivery_system.errors import (
    DuplicateRightsRecord,
    RightsDataError,
    UnknownAuthorization,
)
from delivery_system.models import SourceRef
from delivery_system.rights_engine import RightsEngine
from delivery_system.rights_models import (
    BlockRef,
    Scope,
    Ticket,
)
from delivery_system.rights_repository import RightsRepository

VENUE = "hall_north"
LANGUAGE = "EN"
DAY = "2026-12-01"
PURPOSE = "reading_tour"
HOLDER = "holder"
JIA = "jia"
YI = "yi"
BING = "bing"


def block(name: str = "B1", segment: str = "S1", revision: int = 1) -> BlockRef:
    return BlockRef(name, (SourceRef(segment, revision),))


def broad_scope() -> Scope:
    return Scope(
        venue=VENUE,
        language=LANGUAGE,
        date_from="2026-11-01",
        date_to="2026-12-31",
        purpose=PURPOSE,
    )


class ScopeTests(unittest.TestCase):
    def test_wildcard_yields_other_side(self):
        wild = Scope()
        concrete = broad_scope()
        self.assertEqual(Scope.intersect(wild, concrete), concrete)
        self.assertEqual(Scope.intersect(concrete, wild), concrete)

    def test_dates_take_tighter_bounds(self):
        a = Scope(date_from="2026-11-01", date_to="2026-12-31")
        b = Scope(date_from="2026-11-15", date_to="2026-12-10")
        joined = Scope.intersect(a, b)
        self.assertEqual(joined.date_from, "2026-11-15")
        self.assertEqual(joined.date_to, "2026-12-10")

    def test_incompatible_concrete_values_become_empty(self):
        a = Scope(venue="hall_north")
        b = Scope(venue="hall_south")
        self.assertTrue(Scope.intersect(a, b).is_empty)

    def test_reversed_window_is_empty(self):
        a = Scope(date_from="2026-12-20", date_to="2026-12-10")
        b = broad_scope()
        self.assertTrue(Scope.intersect(a, b).is_empty)

    def test_contains_inclusive_endpoints_and_purpose(self):
        scope = Scope(
            venue=VENUE, language=LANGUAGE,
            date_from="2026-12-01", date_to="2026-12-01", purpose=PURPOSE,
        )
        self.assertTrue(
            scope.contains(venue=VENUE, language=LANGUAGE, day="2026-12-01", purpose=PURPOSE)
        )
        self.assertFalse(
            scope.contains(venue=VENUE, language=LANGUAGE, day="2026-12-02", purpose=PURPOSE)
        )
        self.assertFalse(
            scope.contains(venue=VENUE, language=LANGUAGE, day="2026-12-01", purpose="other")
        )


class BlockIdentityTests(unittest.TestCase):
    def test_identity_is_strip_plus_all_revisions(self):
        a = BlockRef("B1", (SourceRef("S1", 1), SourceRef("S2", 1)))
        same = BlockRef("B1", (SourceRef("S2", 1), SourceRef("S1", 1)))  # reordered
        other_strip = BlockRef("B9", (SourceRef("S1", 1), SourceRef("S2", 1)))
        other_rev = BlockRef("B1", (SourceRef("S1", 2), SourceRef("S2", 1)))
        self.assertTrue(a.matches(same))
        self.assertFalse(a.matches(other_strip))
        self.assertFalse(a.matches(other_rev))

    def test_from_chunk_uses_strip_and_parallel_revisions(self):
        chunk = {
            "translation_id": "T12",
            "source_ids": ["S1", "S2"],
            "source_revisions": [1, 1],
            "text": "x",
        }
        ref = BlockRef.from_chunk(chunk)
        self.assertEqual(ref.strip_id, "T12")
        self.assertEqual(ref.ref_set, frozenset({("S1", 1), ("S2", 1)}))


class ChainEngineTestBase(unittest.TestCase):
    def setUp(self):
        self.b1 = block("B1", "S1", 1)
        self.b2 = block("B2", "S2", 1)

    def build_engine(
        self,
        rights=("P", "Q"),
        *,
        root_sublicensable=True,
        jia_scope=None,
        yi_scope=None,
        bing_scope=None,
    ):
        from delivery_system.rights_models import RootGrant, Delegation, UsageRegistration

        registrations = [
            UsageRegistration("REG1", "BOOK", self.b1, rights),
            UsageRegistration("REG2", "BOOK", self.b2, rights),
        ]
        roots = []
        delegations = []
        for reg_id in ("REG1", "REG2"):
            for right in rights:
                gid = f"ROOT_{reg_id}_{right}"
                roots.append(
                    RootGrant(
                        gid, "BOOK", reg_id, right, HOLDER,
                        scope=broad_scope(), sublicensable=root_sublicensable,
                    )
                )
        delegations.append(
            Delegation(
                "D_JIA", "BOOK", "ROOT_REG1_P", JIA,
                scope=jia_scope or broad_scope(), sublicensable=True,
            )
        )
        delegations.append(
            Delegation(
                "D_YI", "BOOK", "ROOT_REG1_P", YI,
                scope=yi_scope or broad_scope(), sublicensable=True,
            )
        )
        delegations.append(
            Delegation(
                "D_BING", "BOOK", "ROOT_REG1_Q", BING,
                scope=bing_scope or broad_scope(), sublicensable=True,
            )
        )
        return RightsEngine(registrations, roots, delegations)


class ChainTests(ChainEngineTestBase):
    def test_can_sign_and_chain_from_holder(self):
        engine = self.build_engine()
        answer = engine.can_sign(
            JIA, "BOOK", self.b1, "P",
            venue=VENUE, language=LANGUAGE, day=DAY, purpose=PURPOSE,
        )
        self.assertTrue(answer.can_sign)
        self.assertEqual(len(answer.chains), 1)
        chain = answer.chains[0]
        self.assertEqual(chain.holder, HOLDER)
        self.assertEqual(chain.signer, JIA)
        self.assertEqual([link.kind for link in chain.links], ["root", "delegation"])
        self.assertEqual([link.id for link in chain.links], ["ROOT_REG1_P", "D_JIA"])

    def test_holder_itself_is_a_signer_with_zero_hop_chain(self):
        engine = self.build_engine()
        answer = engine.can_sign(
            HOLDER, "BOOK", self.b1, "P",
            venue=VENUE, language=LANGUAGE, day=DAY, purpose=PURPOSE,
        )
        self.assertTrue(answer.can_sign)
        self.assertEqual(len(answer.chains[0].links), 1)
        self.assertEqual(answer.chains[0].links[0].kind, "root")

    def test_other_sources_lists_independent_issuers(self):
        engine = self.build_engine()
        answer = engine.can_sign(
            JIA, "BOOK", self.b1, "P",
            venue=VENUE, language=LANGUAGE, day=DAY, purpose=PURPOSE,
        )
        self.assertEqual({p.signer for p in answer.other_sources}, {HOLDER, YI})

    def test_scope_intersection_denies_outside_window(self):
        narrow = Scope(
            venue=VENUE, language=LANGUAGE,
            date_from="2026-11-10", date_to="2026-11-20", purpose=PURPOSE,
        )
        engine = self.build_engine(jia_scope=narrow)
        inside = engine.can_sign(
            JIA, "BOOK", self.b1, "P",
            venue=VENUE, language=LANGUAGE, day="2026-11-15", purpose=PURPOSE,
        )
        outside = engine.can_sign(
            JIA, "BOOK", self.b1, "P",
            venue=VENUE, language=LANGUAGE, day=DAY, purpose=PURPOSE,
        )
        self.assertTrue(inside.can_sign)
        self.assertFalse(outside.can_sign)

    def test_wrong_venue_or_language_denied(self):
        engine = self.build_engine()
        self.assertFalse(
            engine.can_sign(
                JIA, "BOOK", self.b1, "P",
                venue="hall_south", language=LANGUAGE, day=DAY, purpose=PURPOSE,
            ).can_sign
        )
        self.assertFalse(
            engine.can_sign(
                JIA, "BOOK", self.b1, "P",
                venue=VENUE, language="FR", day=DAY, purpose=PURPOSE,
            ).can_sign
        )

    def test_non_sublicensable_root_makes_child_void(self):
        engine = self.build_engine(root_sublicensable=False)
        answer = engine.can_sign(
            JIA, "BOOK", self.b1, "P",
            venue=VENUE, language=LANGUAGE, day=DAY, purpose=PURPOSE,
        )
        self.assertFalse(answer.can_sign)
        self.assertEqual(
            engine.explain_delegation("D_JIA"),
            "root grant 'ROOT_REG1_P' is not sublicensable",
        )

    def test_non_sublicensable_delegation_blocks_grandchild(self):
        from delivery_system.rights_models import Delegation

        engine = self.build_engine()
        # Make JIA a non-sublicensable trustee; a grandchild beneath JIA is void.
        engine.delegations["D_JIA"] = Delegation(
            "D_JIA", "BOOK", "ROOT_REG1_P", JIA,
            scope=broad_scope(), sublicensable=False,
        )
        engine.delegations["D_GRAND"] = Delegation(
            "D_GRAND", "BOOK", "D_JIA", "sub_vendor",
            scope=broad_scope(), sublicensable=True,
        )
        engine._rebuild_indexes()
        self.assertFalse(
            engine.can_sign(
                "sub_vendor", "BOOK", self.b1, "P",
                venue=VENUE, language=LANGUAGE, day=DAY, purpose=PURPOSE,
            ).can_sign
        )
        self.assertEqual(
            engine.explain_delegation("D_GRAND"),
            "grantor delegation 'D_JIA' is not sublicensable",
        )

    def test_revocation_cuts_only_paths_through_it(self):
        from delivery_system.rights_models import Delegation

        # Two independent delegations of the same root right, plus YI's path.
        delegations = [
            Delegation("D_JIA", "BOOK", "ROOT_REG1_P", JIA, scope=broad_scope()),
            Delegation("D_JIA2", "BOOK", "ROOT_REG1_P", "jia_alt", scope=broad_scope()),
            Delegation("D_YI", "BOOK", "ROOT_REG1_P", YI, scope=broad_scope(), revoked=True),
            Delegation("D_BING", "BOOK", "ROOT_REG1_Q", BING, scope=broad_scope()),
        ]
        from delivery_system.rights_models import RootGrant, UsageRegistration

        registrations = [
            UsageRegistration("REG1", "BOOK", self.b1, ("P", "Q")),
            UsageRegistration("REG2", "BOOK", self.b2, ("P", "Q")),
        ]
        roots = []
        for reg_id in ("REG1", "REG2"):
            for right in ("P", "Q"):
                roots.append(
                    RootGrant(
                        f"ROOT_{reg_id}_{right}", "BOOK", reg_id, right, HOLDER,
                        scope=broad_scope(),
                    )
                )
        engine = RightsEngine(registrations, roots, delegations)
        # YI path gone (revoked); JIA's two independent delegations remain.
        self.assertFalse(
            engine.can_sign(
                YI, "BOOK", self.b1, "P",
                venue=VENUE, language=LANGUAGE, day=DAY, purpose=PURPOSE,
            ).can_sign
        )
        self.assertTrue(
            engine.can_sign(
                JIA, "BOOK", self.b1, "P",
                venue=VENUE, language=LANGUAGE, day=DAY, purpose=PURPOSE,
            ).can_sign
        )
        self.assertTrue(
            engine.can_sign(
                "jia_alt", "BOOK", self.b1, "P",
                venue=VENUE, language=LANGUAGE, day=DAY, purpose=PURPOSE,
            ).can_sign
        )
        self.assertEqual(engine.explain_delegation("D_YI"), "revoked")

    def test_unknown_block_and_unknown_right_return_no_chain(self):
        engine = self.build_engine()
        stranger = block("B99", "S99", 1)
        self.assertEqual(
            engine.chains_for(
                "BOOK", stranger, "P",
                venue=VENUE, language=LANGUAGE, day=DAY, purpose=PURPOSE,
            ),
            [],
        )
        self.assertEqual(
            engine.chains_for(
                "BOOK", self.b1, "ZZ",
                venue=VENUE, language=LANGUAGE, day=DAY, purpose=PURPOSE,
            ),
            [],
        )

    def test_cyclic_delegations_do_not_recurse_forever(self):
        from delivery_system.rights_models import Delegation

        engine = self.build_engine()
        # Hand-build a cycle D_JIA -> D_LOOP -> D_JIA beneath the P root.
        engine.delegations["D_LOOP"] = Delegation(
            "D_LOOP", "BOOK", "D_JIA", "loop_vendor", scope=broad_scope()
        )
        engine.delegations["D_BACK"] = Delegation(
            "D_BACK", "BOOK", "D_LOOP", JIA, scope=broad_scope()
        )
        engine._rebuild_indexes()
        # Terminates (no infinite recursion) and the direct JIA chain is still
        # present with its normal two-link shape.
        answer = engine.can_sign(
            JIA, "BOOK", self.b1, "P",
            venue=VENUE, language=LANGUAGE, day=DAY, purpose=PURPOSE,
        )
        self.assertTrue(answer.can_sign)
        direct = next(
            c for c in answer.chains
            if [l.id for l in c.links] == ["ROOT_REG1_P", "D_JIA"]
        )
        self.assertEqual(direct.signer, JIA)
        # No chain repeats a delegation id (cycles are pruned, not emitted).
        for chain in answer.chains:
            ids = [l.id for l in chain.links if l.kind == "delegation"]
            self.assertEqual(len(ids), len(set(ids)))

    def test_multiple_registrations_on_one_block_all_contribute(self):
        from delivery_system.rights_models import (
            Delegation,
            RootGrant,
            UsageRegistration,
        )

        registrations = [UsageRegistration("REG1", "BOOK", self.b1, ("P",))]
        roots = [
            RootGrant("ROOT1_P", "BOOK", "REG1", "P", HOLDER, scope=broad_scope()),
        ]
        # A second independent registration of the same block adds right R.
        registrations.append(UsageRegistration("REG2", "BOOK", self.b1, ("R",)))
        roots.append(RootGrant("ROOT2_R", "BOOK", "REG2", "R", "holder_two", scope=broad_scope()))
        delegations = [
            Delegation("D1", "BOOK", "ROOT1_P", JIA, scope=broad_scope()),
            Delegation("D2", "BOOK", "ROOT2_R", JIA, scope=broad_scope()),
        ]
        engine = RightsEngine(registrations, roots, delegations)
        self.assertTrue(
            engine.can_sign(
                JIA, "BOOK", self.b1, "P",
                venue=VENUE, language=LANGUAGE, day=DAY, purpose=PURPOSE,
            ).can_sign
        )
        self.assertTrue(
            engine.can_sign(
                JIA, "BOOK", self.b1, "R",
                venue=VENUE, language=LANGUAGE, day=DAY, purpose=PURPOSE,
            ).can_sign
        )


class TicketPlanningTests(ChainEngineTestBase):
    def _engine(self):
        return self.build_engine()

    def test_whole_ticket_paid_once_covers_many_cells(self):
        from delivery_system.rights_models import Delegation, RootGrant, UsageRegistration

        registrations = [
            UsageRegistration("REG1", "BOOK", self.b1, ("P", "Q")),
            UsageRegistration("REG2", "BOOK", self.b2, ("P", "Q")),
        ]
        roots = []
        delegations = []
        for reg_id in ("REG1", "REG2"):
            for right in ("P", "Q"):
                root_id = f"ROOT_{reg_id}_{right}"
                roots.append(
                    RootGrant(root_id, "BOOK", reg_id, right, HOLDER, scope=broad_scope())
                )
                # JIA signs every right of both blocks through explicit chains.
                delegations.append(
                    Delegation(f"D_JIA_{root_id}", "BOOK", root_id, JIA, scope=broad_scope())
                )
        # YI/BING exist as alternatives for a single right each.
        delegations.append(
            Delegation("D_YI_P", "BOOK", "ROOT_REG1_P", YI, scope=broad_scope())
        )
        delegations.append(
            Delegation("D_BING_Q", "BOOK", "ROOT_REG1_Q", BING, scope=broad_scope())
        )
        engine = RightsEngine(registrations, roots, delegations)

        tickets = [
            Ticket("SEASON", JIA, ("P", "Q"), (DAY,), 3),
            Ticket("LOOSE_P", YI, ("P",), (DAY,), 2),
            Ticket("LOOSE_Q", BING, ("Q",), (DAY,), 2),
        ]
        plan = engine.plan_tickets(
            "BOOK", [self.b1, self.b2], venue=VENUE, language=LANGUAGE,
            days=[DAY], tickets=tickets, purpose=PURPOSE,
        )
        self.assertTrue(plan.ok)
        self.assertEqual(plan.selected_ticket_ids, ("SEASON",))
        self.assertEqual(plan.total_fee, 3)  # one fee, not per cell
        self.assertEqual(len(plan.credentials), 4)  # 2 blocks x 2 rights

    def test_worked_example_three_then_four_then_two(self):
        from delivery_system.rights_models import Delegation, RootGrant, UsageRegistration

        registrations = [
            UsageRegistration("REG1", "BOOK", self.b1, ("P", "Q")),
            UsageRegistration("REG2", "BOOK", self.b2, ("P", "Q")),
        ]
        roots = []
        for reg_id in ("REG1", "REG2"):
            for right in ("P", "Q"):
                roots.append(
                    RootGrant(
                        f"ROOT_{reg_id}_{right}", "BOOK", reg_id, right, HOLDER,
                        scope=broad_scope(),
                    )
                )

        def delegations(jia_q_revoked: bool):
            links = []
            for reg_id in ("REG1", "REG2"):
                # JIA receives P and Q over separate delegations.
                links.append(
                    Delegation(f"D_JIA_{reg_id}_P", "BOOK", f"ROOT_{reg_id}_P",
                               JIA, scope=broad_scope())
                )
                links.append(
                    Delegation(f"D_JIA_{reg_id}_Q", "BOOK", f"ROOT_{reg_id}_Q",
                               JIA, scope=broad_scope(), revoked=jia_q_revoked)
                )
                # YI only P; BING only Q.
                links.append(
                    Delegation(f"D_YI_{reg_id}_P", "BOOK", f"ROOT_{reg_id}_P",
                               YI, scope=broad_scope())
                )
                links.append(
                    Delegation(f"D_BING_{reg_id}_Q", "BOOK", f"ROOT_{reg_id}_Q",
                               BING, scope=broad_scope())
                )
            return links

        def tickets(jia_fee, loose_fee):
            return [
                Ticket("T_JIA", JIA, ("P", "Q"), (DAY,), jia_fee),
                Ticket("T_YI", YI, ("P",), (DAY,), loose_fee),
                Ticket("T_BING", BING, ("Q",), (DAY,), loose_fee),
            ]

        baseline = RightsEngine(registrations, roots, delegations(False)).plan_tickets(
            "BOOK", [self.b1, self.b2], venue=VENUE, language=LANGUAGE,
            days=[DAY], tickets=tickets(3, 2), purpose=PURPOSE,
        )
        self.assertEqual(baseline.selected_ticket_ids, ("T_JIA",))
        self.assertEqual(baseline.total_fee, 3)

        # Revoke JIA's Q delegations; his ticket now signs P only.
        revoked = RightsEngine(registrations, roots, delegations(True)).plan_tickets(
            "BOOK", [self.b1, self.b2], venue=VENUE, language=LANGUAGE,
            days=[DAY], tickets=tickets(3, 2), purpose=PURPOSE,
        )
        self.assertTrue(revoked.ok)
        self.assertEqual(set(revoked.selected_ticket_ids), {"T_YI", "T_BING"})
        self.assertEqual(revoked.total_fee, 4)
        self.assertTrue(all(c.vendor != JIA for c in revoked.credentials))

        # Same authorizations, only lower scattered prices: optimum is fee 2.
        repriced = RightsEngine(registrations, roots, delegations(True)).plan_tickets(
            "BOOK", [self.b1, self.b2], venue=VENUE, language=LANGUAGE,
            days=[DAY], tickets=tickets(3, 1), purpose=PURPOSE,
        )
        self.assertEqual(set(repriced.selected_ticket_ids), {"T_YI", "T_BING"})
        self.assertEqual(repriced.total_fee, 2)

    def test_ticket_ignores_rights_vendor_cannot_sign(self):
        engine = self._engine()
        # YI can only sign P, yet his ticket claims P and Q. The Q cells must
        # not be credited to it.
        tickets = [
            Ticket("OVERCLAIM", YI, ("P", "Q"), (DAY,), 1),
            Ticket("Q_ONLY", BING, ("Q",), (DAY,), 1),
        ]
        plan = engine.plan_tickets(
            "BOOK", [self.b1], venue=VENUE, language=LANGUAGE,
            days=[DAY], tickets=tickets, purpose=PURPOSE,
        )
        self.assertTrue(plan.ok)
        self.assertEqual(set(plan.selected_ticket_ids), {"OVERCLAIM", "Q_ONLY"})
        q_credential = next(c for c in plan.credentials if c.atom.right == "Q")
        self.assertEqual(q_credential.vendor, BING)

    def test_uncoverable_session_is_refused_not_licensed(self):
        engine = self._engine()
        tickets = [Ticket("P_ONLY", YI, ("P",), (DAY,), 1)]
        plan = engine.plan_tickets(
            "BOOK", [self.b1], venue=VENUE, language=LANGUAGE,
            days=[DAY], tickets=tickets, purpose=PURPOSE,
        )
        self.assertFalse(plan.ok)
        self.assertEqual(plan.reason, "uncovered_requirements")
        self.assertEqual(plan.selected_ticket_ids, ())
        self.assertIsNone(plan.total_fee)
        self.assertTrue(all(atom.right == "Q" for atom in plan.uncovered))

    def test_mutex_group_allows_at_most_one_ticket(self):
        engine = self._engine()
        tickets = [
            # Two P tickets in the same quote group; only one may be bought.
            Ticket("G1_A", YI, ("P",), (DAY,), 5, group="G1"),
            Ticket("G1_B", YI, ("P",), (DAY,), 2, group="G1"),
            Ticket("G2_Q", BING, ("Q",), (DAY,), 1, group="G2"),
        ]
        plan = engine.plan_tickets(
            "BOOK", [self.b1], venue=VENUE, language=LANGUAGE,
            days=[DAY], tickets=tickets, purpose=PURPOSE,
        )
        self.assertTrue(plan.ok)
        self.assertEqual(set(plan.selected_ticket_ids), {"G1_B", "G2_Q"})
        self.assertEqual(plan.total_fee, 3)

    def test_multi_day_coverage_and_day_window(self):
        engine = self._engine()
        tickets = [
            Ticket("SEASON", YI, ("P",), ("2026-12-01", "2026-12-02"), 4),
            Ticket("DAY1", YI, ("P",), ("2026-12-01",), 1),
            Ticket("DAY2", YI, ("P",), ("2026-12-02",), 1),
        ]
        # Only P required on this block to isolate the day dimension.
        from delivery_system.rights_models import UsageRegistration

        engine.registrations["REG1"] = UsageRegistration("REG1", "BOOK", self.b1, ("P",))
        plan = engine.plan_tickets(
            "BOOK", [self.b1], venue=VENUE, language=LANGUAGE,
            days=["2026-12-01", "2026-12-02"], tickets=tickets, purpose=PURPOSE,
        )
        self.assertTrue(plan.ok)
        self.assertEqual(set(plan.selected_ticket_ids), {"DAY1", "DAY2"})
        self.assertEqual(plan.total_fee, 2)

    def test_same_text_different_strip_cannot_borrow_registration(self):
        engine = self._engine()
        fraud = BlockRef("B1_FRAUD", self.b1.refs)  # identical refs, new strip
        plan = engine.plan_tickets(
            "BOOK", [fraud], venue=VENUE, language=LANGUAGE,
            days=[DAY], tickets=[Ticket("T", YI, ("P", "Q"), (DAY,), 1)],
            purpose=PURPOSE,
        )
        self.assertFalse(plan.ok)
        self.assertEqual(plan.reason, "unregistered_blocks")
        self.assertEqual(plan.unregistered_blocks[0].strip_id, "B1_FRAUD")

    def test_legacy_book_without_rights_data_is_free(self):
        engine = RightsEngine([], [], [], legacy_document_ids=["OLD"])
        plan = engine.plan_tickets(
            "OLD", [self.b1], venue=VENUE, language=LANGUAGE,
            days=[DAY], tickets=[],
        )
        self.assertTrue(plan.ok)
        self.assertEqual(plan.total_fee, 0)
        self.assertEqual(plan.requirements, ())


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = RightsRepository(os.path.join(self._tmp.name, "rights.db"))
        self.b1 = block("B1", "S1", 1)

    def tearDown(self):
        self._tmp.cleanup()

    def test_roundtrip_and_revocation_rebuilds_chains(self):
        self.repo.register_usage("REG1", "BOOK", self.b1, ("P",))
        self.repo.add_root_grant("ROOT1", "BOOK", "REG1", "P", HOLDER, scope=broad_scope())
        self.repo.add_delegation("D1", "BOOK", "ROOT1", JIA, scope=broad_scope())
        engine = self.repo.engine()
        self.assertTrue(
            engine.can_sign(
                JIA, "BOOK", self.b1, "P",
                venue=VENUE, language=LANGUAGE, day=DAY, purpose=PURPOSE,
            ).can_sign
        )
        self.repo.revoke_delegation("D1")
        self.assertFalse(
            self.repo.engine().can_sign(
                JIA, "BOOK", self.b1, "P",
                venue=VENUE, language=LANGUAGE, day=DAY, purpose=PURPOSE,
            ).can_sign
        )

    def test_dangling_parent_rejected(self):
        self.repo.register_usage("REG1", "BOOK", self.b1, ("P",))
        with self.assertRaises(UnknownAuthorization):
            self.repo.add_delegation("DANGLING", "BOOK", "NO_ROOT", JIA)

    def test_root_for_unregistered_right_rejected(self):
        with self.assertRaises(UnknownAuthorization):
            self.repo.add_root_grant("ROOT1", "BOOK", "GHOST", "P", HOLDER)

    def test_duplicate_id_with_different_content_rejected(self):
        self.repo.register_usage("REG1", "BOOK", self.b1, ("P",))
        other = block("B2", "S2", 1)
        with self.assertRaises(DuplicateRightsRecord):
            self.repo.register_usage("REG1", "BOOK", other, ("P",))

    def test_idempotent_re_add_identical_ticket(self):
        self.repo.add_ticket("T1", JIA, ("P",), (DAY,), 3, group="G")
        self.repo.add_ticket("T1", JIA, ("P",), (DAY,), 3, group="G")  # no error
        with self.assertRaises(DuplicateRightsRecord):
            self.repo.add_ticket("T1", JIA, ("P",), (DAY,), 4, group="G")


class EndToEndPackageTests(unittest.TestCase):
    def test_clearance_runs_over_real_frozen_package(self):
        from delivery_system.delivery_repository import DeliveryRepository
        from delivery_system.rights_demo import (
            HOLDER as DEMO_HOLDER,
            VENDOR_BING,
            VENDOR_JIA,
            VENDOR_YI,
            _build_real_package,
            _register_worked_rights,
        )
        from delivery_system.rights_models import Ticket as T

        with tempfile.TemporaryDirectory() as tmp:
            _, _, package, _ = _build_real_package(tmp)
            blocks = blocks_from_package(package)
            rights = _register_worked_rights(os.path.join(tmp, "rights.db"), blocks)
            engine = rights.engine()
            tickets = [
                T("T_JIA", VENDOR_JIA, ("P", "Q"), (DAY,), 3),
                T("T_YI", VENDOR_YI, ("P",), (DAY,), 2),
                T("T_BING", VENDOR_BING, ("Q",), (DAY,), 2),
            ]
            plan = plan_screening(
                engine, package, venue=VENUE, days=[DAY],
                tickets=tickets, purpose=PURPOSE,
            )
            self.assertTrue(plan.ok)
            self.assertEqual(plan.selected_ticket_ids, ("T_JIA",))
            answer = screen_block(
                engine, package, blocks[0], "P", VENDOR_JIA,
                venue=VENUE, day=DAY, purpose=PURPOSE,
            )
            self.assertTrue(answer.can_sign)
            self.assertEqual(answer.chains[0].holder, DEMO_HOLDER)


if __name__ == "__main__":
    unittest.main()
