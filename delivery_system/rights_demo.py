"""End-to-end rights + ticketing demonstration over real translation packages.

The first half builds actual ``translation-package/1`` packages through the
existing catalog/repository pipeline, then clears the *actual blocks* of those
packages.  The scenario reproduces the worked brief:

* two blocks, each needing rights P and Q;
* vendor 甲 holds both through separate delegations and sells one whole ticket;
* vendors 乙 / 丙 each sell a single-right scattered ticket;
* revoking 甲's Q delegation leaves its ticket valid for P only, so the cheapest
  purchase becomes the two scattered tickets;
* keeping every authorization fixed and only dropping the scattered prices
  flips the optimum back to the scattered pair.

The second half shows independent paths: revoking one delegation leaves an
unrelated delegation carrying the same right to a different signer.  Finally a
same-text/different-strip package is refused, and a legacy book without rights
data stays readable.
"""

from __future__ import annotations

import os
from typing import Any

from .clearance import blocks_from_package, plan_screening, screen_block
from .delivery_repository import DeliveryRepository
from .models import SourceRef
from .package_io import write_package
from .rights_repository import RightsRepository
from .rights_models import BlockRef, Scope, Ticket
from .source_catalog import PublishedSourceReader, SourceCatalog

VENDOR_JIA = "vendor_jia"
VENDOR_YI = "vendor_yi"
VENDOR_BING = "vendor_bing"
HOLDER = "original_holder"
HOLDER_TWO = "second_holder"
VENUE = "hall_north"
LANGUAGE = "EN"
DAY = "2026-12-01"
PURPOSE = "reading_tour"


def _build_real_package(out_dir: str) -> tuple[SourceCatalog, DeliveryRepository, dict[str, Any], str]:
    """Drive the existing pipeline to produce a real two-block package."""

    source_db = os.path.join(out_dir, "source.db")
    delivery_db = os.path.join(out_dir, "delivery.db")
    packages_dir = os.path.join(out_dir, "packages")
    os.makedirs(packages_dir, exist_ok=True)

    catalog = SourceCatalog(source_db)
    repo = DeliveryRepository(delivery_db, PublishedSourceReader(source_db))
    catalog.create_document(
        "BOOK",
        [
            {"segment_id": "S1", "revision": 1, "text": "First source."},
            {"segment_id": "S2", "revision": 1, "text": "Second source."},
        ],
    )
    repo.add_translation(
        "B1", "BOOK", LANGUAGE, "First translation.",
        [{"segment_id": "S1", "revision": 1}],
    )
    repo.add_translation(
        "B2", "BOOK", LANGUAGE, "Second translation.",
        [{"segment_id": "S2", "revision": 1}],
    )
    package = repo.publish_language("BOOK", LANGUAGE, "BOOKPACK")
    path = os.path.join(packages_dir, "BOOKPACK.json")
    write_package(package, path)
    return catalog, repo, package, path


def _register_worked_rights(rights_db: str, blocks: list[BlockRef]) -> RightsRepository:
    rights = RightsRepository(rights_db)
    block_one, block_two = blocks
    rights.register_usage("REG1", "BOOK", block_one, ("P", "Q"))
    rights.register_usage("REG2", "BOOK", block_two, ("P", "Q"))

    broad = Scope(venue=VENUE, language=LANGUAGE, date_from="2026-11-01", date_to="2026-12-31",
                  purpose=PURPOSE)
    # Original roots for every required right (both blocks list both rights).
    for reg_id in ("REG1", "REG2"):
        rights.add_root_grant(f"ROOT_{reg_id}_P", "BOOK", reg_id, "P", HOLDER, scope=broad)
        rights.add_root_grant(f"ROOT_{reg_id}_Q", "BOOK", reg_id, "Q", HOLDER, scope=broad)

    # 甲 receives P and Q over two separate delegations (two independent chains).
    for reg_id in ("REG1", "REG2"):
        rights.add_delegation(
            f"D_JIA_{reg_id}_P", "BOOK", f"ROOT_{reg_id}_P", VENDOR_JIA, scope=broad
        )
        rights.add_delegation(
            f"D_JIA_{reg_id}_Q", "BOOK", f"ROOT_{reg_id}_Q", VENDOR_JIA, scope=broad
        )
    # 乙 signs only P; 丙 signs only Q.
    for reg_id in ("REG1", "REG2"):
        rights.add_delegation(
            f"D_YI_{reg_id}_P", "BOOK", f"ROOT_{reg_id}_P", VENDOR_YI, scope=broad
        )
        rights.add_delegation(
            f"D_BING_{reg_id}_Q", "BOOK", f"ROOT_{reg_id}_Q", VENDOR_BING, scope=broad
        )
    return rights


def _worked_tickets(jia_fee: float, loose_fee: float) -> list[Ticket]:
    return [
        # Whole ticket: fixed fee regardless of how many blocks/rights it covers.
        Ticket("T_JIA", VENDOR_JIA, ("P", "Q"), (DAY,), jia_fee),
        Ticket("T_YI_P", VENDOR_YI, ("P",), (DAY,), loose_fee),
        Ticket("T_BING_Q", VENDOR_BING, ("Q",), (DAY,), loose_fee),
    ]


def run_rights_demo(out_dir: str) -> dict[str, Any]:
    os.makedirs(out_dir, exist_ok=True)
    report: dict[str, Any] = {"ok": True, "steps": [], "checks": []}

    def step(name: str, payload: dict[str, Any]) -> None:
        report["steps"].append({"name": name, **payload})

    def check(name: str, condition: bool, detail: Any = None) -> None:
        report["checks"].append({"name": name, "ok": bool(condition), "detail": detail})
        if not condition:
            report["ok"] = False

    catalog, repo, package, package_path = _build_real_package(out_dir)
    blocks = blocks_from_package(package)
    step(
        "real_package_built",
        {
            "package_path": package_path,
            "blocks": [b.to_dict() for b in blocks],
            "strip_ids": [b.strip_id for b in blocks],
        },
    )
    check("package_has_two_actual_blocks", len(blocks) == 2)
    check("blocks_are_strips_B1_B2", [b.strip_id for b in blocks] == ["B1", "B2"])

    rights_db = os.path.join(out_dir, "rights.db")
    rights = _register_worked_rights(rights_db, blocks)
    engine = rights.engine()

    # Baseline: 甲's whole ticket (3) beats the two scattered tickets (2+2=4).
    tickets = _worked_tickets(jia_fee=3, loose_fee=2)
    baseline = plan_screening(
        engine, package, venue=VENUE, days=[DAY], tickets=tickets, purpose=PURPOSE
    )
    step(
        "baseline_plan",
        {"ok": baseline.ok, "total_fee": baseline.total_fee,
         "selected": list(baseline.selected_ticket_ids)},
    )
    check("baseline_ok", baseline.ok)
    check("baseline_picks_jia_fee_3", baseline.selected_ticket_ids == ("T_JIA",),
          baseline.selected_ticket_ids)
    check("baseline_fee_3", baseline.total_fee == 3)
    check(
        "baseline_every_cell_credentialed",
        len(baseline.credentials) == len(baseline.requirements),
    )

    # The rights desk answers a single signing question and shows the chain.
    answer = screen_block(
        engine, package, blocks[0], "P", VENDOR_JIA, venue=VENUE, day=DAY, purpose=PURPOSE
    )
    step(
        "rights_desk_question",
        {"can_sign": answer.can_sign, "answer": answer.to_dict()},
    )
    check("jia_can_sign_P", answer.can_sign)
    check(
        "chain_starts_at_original_holder",
        answer.chains
        and answer.chains[0].holder == HOLDER
        and [link.kind for link in answer.chains[0].links] == ["root", "delegation"],
    )
    check(
        "other_sources_yi_and_holder_also_sign_P",
        {path.signer for path in answer.other_sources} == {HOLDER, VENDOR_YI},
        [path.signer for path in answer.other_sources],
    )

    # Revoke 甲's Q delegations. His ticket now signs P only.
    for reg_id in ("REG1", "REG2"):
        rights.revoke_delegation(f"D_JIA_{reg_id}_Q")
    engine_after = rights.engine()
    q_answer = screen_block(
        engine_after, package, blocks[0], "Q", VENDOR_JIA,
        venue=VENUE, day=DAY, purpose=PURPOSE,
    )
    check("jia_can_no_longer_sign_Q", not q_answer.can_sign)
    check(
        "bing_and_holder_are_other_source_for_Q",
        {path.signer for path in q_answer.other_sources} == {HOLDER, VENDOR_BING},
        [path.signer for path in q_answer.other_sources],
    )
    check(
        "revocation_reason_recorded",
        engine_after.explain_delegation("D_JIA_REG1_Q") == "revoked",
    )

    revoked_plan = plan_screening(
        engine_after, package, venue=VENUE, days=[DAY],
        tickets=_worked_tickets(jia_fee=3, loose_fee=2), purpose=PURPOSE,
    )
    step(
        "after_revoke_plan",
        {"ok": revoked_plan.ok, "total_fee": revoked_plan.total_fee,
         "selected": list(revoked_plan.selected_ticket_ids),
         "credentials": [c.to_dict() for c in revoked_plan.credentials]},
    )
    check("revoked_plan_ok", revoked_plan.ok)
    check(
        "revoked_picks_two_scattered_fee_4",
        revoked_plan.selected_ticket_ids == ("T_BING_Q", "T_YI_P"),
        revoked_plan.selected_ticket_ids,
    )
    check("revoked_fee_4", revoked_plan.total_fee == 4)
    # The whole ticket must not be credited for a right it can no longer sign.
    jia_credentials = [c for c in revoked_plan.credentials if c.vendor == VENDOR_JIA]
    check("jia_ticket_not_used_after_q_revoke", jia_credentials == [])
    check(
        "each_credential_carries_full_chain",
        all(c.chain.links[0].kind == "root" for c in revoked_plan.credentials),
    )

    # Keep authorizations fixed; only lower the scattered prices 2 -> 1.
    repriced = plan_screening(
        engine_after, package, venue=VENUE, days=[DAY],
        tickets=_worked_tickets(jia_fee=3, loose_fee=1), purpose=PURPOSE,
    )
    step(
        "repriced_plan",
        {"ok": repriced.ok, "total_fee": repriced.total_fee,
         "selected": list(repriced.selected_ticket_ids)},
    )
    check("repriced_ok", repriced.ok)
    check(
        "repriced_picks_scattered_pair_fee_2",
        repriced.selected_ticket_ids == ("T_BING_Q", "T_YI_P")
        and repriced.total_fee == 2,
        (repriced.selected_ticket_ids, repriced.total_fee),
    )

    # A refused session: remove 丙's ticket, Q can no longer be covered at all.
    refused_tickets = [t for t in _worked_tickets(jia_fee=3, loose_fee=1)
                       if t.ticket_id != "T_BING_Q"]
    refused = plan_screening(
        engine_after, package, venue=VENUE, days=[DAY],
        tickets=refused_tickets, purpose=PURPOSE,
    )
    step(
        "refused_session",
        {"ok": refused.ok, "reason": refused.reason,
         "uncovered": [a.to_dict() for a in refused.uncovered]},
    )
    check("refused_is_not_licensed", not refused.ok and refused.reason == "uncovered_requirements")
    check("refused_has_no_selected_tickets", refused.selected_ticket_ids == ())

    report["worked_example"] = {
        "baseline": baseline.to_dict(),
        "after_revoke": revoked_plan.to_dict(),
        "repriced": repriced.to_dict(),
        "refused": refused.to_dict(),
    }

    # ------------------------------------------------------------------
    # Independent paths: one right reaching different signers two ways.
    # blocks[0] is the single-source block B1; its dedicated right name RP
    # keeps this registration hermetic from the worked-example rights.
    # ------------------------------------------------------------------
    independent = _independent_path_demo(rights, blocks[0])
    report["independent_paths"] = independent
    check(
        "independent_path_survives_revocation",
        independent["after_revoke"]["can_sign_ding"],
        independent,
    )
    check(
        "independent_path_other_chain_intact",
        set(independent["after_revoke"]["remaining_chain_ids"])
        == {"ROOT_IND_P", "D_IND_DING"},
        independent["after_revoke"],
    )

    # ------------------------------------------------------------------
    # Scope narrowing and non-sublicensable grantors.
    # ------------------------------------------------------------------
    scope_demo = _scope_demo(rights, blocks[1])
    report["scope_demo"] = scope_demo
    check("narrow_scope_outside_window_denied", scope_demo["outside_window_denied"])
    check("non_sublicensable_child_void", scope_demo["void_child_denied"])

    # ------------------------------------------------------------------
    # Same text, different strip: registration must not be borrowed.
    # ------------------------------------------------------------------
    fraud_demo = _fraud_demo(rights, repo, blocks[1])
    report["fraud_demo"] = fraud_demo
    check("different_strip_refused", fraud_demo["refused"])
    check("fraud_reason_unregistered", fraud_demo["reason"] == "unregistered_blocks")

    # ------------------------------------------------------------------
    # Legacy book without rights data stays readable, no clearance needed.
    # ------------------------------------------------------------------
    legacy = _legacy_demo(out_dir)
    report["legacy_demo"] = legacy
    check("legacy_book_clearance_is_free", legacy["ok"] and legacy["total_fee"] == 0)

    return report


def _independent_path_demo(rights: RightsRepository, block: BlockRef) -> dict[str, Any]:
    """Right RP reaches 戊 through D1 and 丁 through a fully independent D2.

    Using a dedicated right name keeps this demonstration hermetic: no chain
    from the worked-example registrations can answer an RP question.
    """

    vendor_wu = "vendor_wu"
    vendor_ding = "vendor_ding"
    broad = Scope(venue=VENUE, language=LANGUAGE, date_from="2026-11-01",
                  date_to="2026-12-31", purpose=PURPOSE)
    rights.register_usage("REG_IND", "BOOK", block, ("RP",))
    rights.add_root_grant("ROOT_IND_P", "BOOK", "REG_IND", "RP", HOLDER_TWO, scope=broad)
    rights.add_delegation("D_IND_WU", "BOOK", "ROOT_IND_P", vendor_wu, scope=broad)
    rights.add_delegation("D_IND_DING", "BOOK", "ROOT_IND_P", vendor_ding, scope=broad)

    engine = rights.engine()
    before = engine.can_sign(
        vendor_ding, "BOOK", block, "RP", venue=VENUE, language=LANGUAGE, day=DAY, purpose=PURPOSE
    )
    # Revoke the path to 戊; the unrelated delegation to 丁 must stay effective.
    rights.revoke_delegation("D_IND_WU")
    engine = rights.engine()
    ding = engine.can_sign(
        vendor_ding, "BOOK", block, "RP", venue=VENUE, language=LANGUAGE, day=DAY, purpose=PURPOSE
    )
    wu = engine.can_sign(
        vendor_wu, "BOOK", block, "RP", venue=VENUE, language=LANGUAGE, day=DAY, purpose=PURPOSE
    )
    remaining = sorted(
        link.id for path in ding.chains for link in path.links
    )
    return {
        "before_revoke": {"can_sign_ding": before.can_sign},
        "after_revoke": {
            "can_sign_ding": ding.can_sign,
            "can_sign_wu": wu.can_sign,
            "remaining_chain_ids": remaining,
            "other_sources_for_ding": [
                path.signer for path in ding.other_sources
            ],
        },
    }


def _scope_demo(rights: RightsRepository, block: BlockRef) -> dict[str, Any]:
    vendor = "vendor_scope"
    subvendor = "vendor_scope_child"
    rights.register_usage("REG_SCOPE", "BOOK", block, ("Q",))
    november = Scope(venue=VENUE, language=LANGUAGE, date_from="2026-11-01",
                     date_to="2026-11-30", purpose=PURPOSE)
    rights.add_root_grant("ROOT_SCOPE_Q", "BOOK", "REG_SCOPE", "Q", HOLDER, scope=november)
    # A delegation that narrows the window further still inside November.
    inner = Scope(venue=VENUE, language=LANGUAGE, date_from="2026-11-10",
                  date_to="2026-11-20", purpose=PURPOSE)
    rights.add_delegation("D_SCOPE", "BOOK", "ROOT_SCOPE_Q", vendor, scope=inner)

    # A non-sublicensable root: its child delegation must be void.
    rights.register_usage("REG_NOSUB", "BOOK", block, ("P",))
    rights.add_root_grant(
        "ROOT_NOSUB_P", "BOOK", "REG_NOSUB", "P", HOLDER,
        scope=november, sublicensable=False,
    )
    rights.add_delegation("D_NOSUB_CHILD", "BOOK", "ROOT_NOSUB_P", subvendor, scope=inner)

    engine = rights.engine()
    inside = engine.can_sign(
        vendor, "BOOK", block, "Q", venue=VENUE, language=LANGUAGE,
        day="2026-11-15", purpose=PURPOSE,
    )
    outside = engine.can_sign(
        vendor, "BOOK", block, "Q", venue=VENUE, language=LANGUAGE,
        day=DAY, purpose=PURPOSE,  # December, outside the November window
    )
    void_child = engine.can_sign(
        subvendor, "BOOK", block, "P", venue=VENUE, language=LANGUAGE,
        day="2026-11-15", purpose=PURPOSE,
    )
    return {
        "inside_window_allowed": inside.can_sign,
        "outside_window_denied": not outside.can_sign,
        "void_child_denied": not void_child.can_sign,
        "void_child_reason": engine.explain_delegation("D_NOSUB_CHILD"),
    }


def _fraud_demo(rights: RightsRepository, repo: DeliveryRepository, block: BlockRef) -> dict[str, Any]:
    """A different strip rendering the same refs cannot borrow B2's registration.

    The text is identical on purpose: clearing must key on strip id + every
    source revision, not on rendered text.
    """

    fraud_strip = BlockRef(strip_id="B2_FRAUD", refs=block.refs)

    engine = rights.engine()
    plan = engine.plan_tickets(
        "BOOK",
        [fraud_strip],
        venue=VENUE,
        language=LANGUAGE,
        days=[DAY],
        tickets=[Ticket("T_ANY", VENDOR_JIA, ("P", "Q"), (DAY,), 1)],
        purpose=PURPOSE,
    )
    return {
        "refused": not plan.ok,
        "reason": plan.reason,
        "unregistered_blocks": [b.to_dict() for b in plan.unregistered_blocks],
        "same_refs_different_strip": fraud_strip.strip_id != block.strip_id,
    }


def _legacy_demo(out_dir: str) -> dict[str, Any]:
    """An old book with no rights data at all remains readable/cleared free."""

    legacy_db = os.path.join(out_dir, "legacy.db")
    legacy_rights = RightsRepository(legacy_db, legacy_document_ids=["OLD_BOOK"])
    legacy_block = BlockRef(strip_id="OLD_T1", refs=(SourceRef("X1", 1),))
    plan = legacy_rights.engine().plan_tickets(
        "OLD_BOOK",
        [legacy_block],
        venue="any_hall",
        language="EN",
        days=[DAY],
        tickets=[],
    )
    return {"ok": plan.ok, "total_fee": plan.total_fee, "requirements": len(plan.requirements)}
