"""End-to-end reproduction of the contrastive-textbook / tile-board scenario.

Two documents are published through the *real* catalog + repository:

* ``PQ_BUNDLE`` -- one entry ``XY`` (source ``P,Q`` -> text ``x y``) forms the
  whole package, so the booklet records a single two-segment pair.
* ``PQ_SPLIT``  -- entries ``X`` (``P`` -> ``x``) and ``Y`` (``Q`` -> ``y``)
  form the package; its rendered text still reads ``x y``.

Booklets are then built from the actual package files via
:func:`analyze_package`, and the dictionary desk / tile board run on them.  The
demo checks every claim in the brief and returns a report.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .delivery_repository import DeliveryRepository
from .package_io import read_package, validate_package, write_package
from .source_catalog import PublishedSourceReader, SourceCatalog
from .textbook import (
    DictionarySpace,
    Exercise,
    analyze_package,
    build_book,
    package_supply,
    solve_allocation,
    verify_package_against_book,
)

DEFAULT_MATERIALS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "materials",
    "textbook",
)


def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _load_jsonl(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _publish(out_dir: str, document_id: str, segments, entries, package_id):
    source_db = os.path.join(out_dir, f"{document_id}.source.db")
    delivery_db = os.path.join(out_dir, f"{document_id}.delivery.db")
    catalog = SourceCatalog(source_db)
    repo = DeliveryRepository(delivery_db, PublishedSourceReader(source_db))
    catalog.create_document(document_id, segments)
    for entry in entries:
        repo.add_translation(
            entry["translation_id"],
            document_id,
            entry["language"],
            entry["text"],
            entry["source_refs"],
        )
    content = repo.publish_language(document_id, "EN", package_id)
    path = os.path.join(out_dir, "packages", f"{package_id}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_package(content, path)
    return content, path, catalog, repo


def run_textbook_demo(
    out_dir: str, materials: str = DEFAULT_MATERIALS
) -> dict[str, Any]:
    os.makedirs(out_dir, exist_ok=True)
    report: dict[str, Any] = {"ok": True, "steps": [], "checks": []}

    def step(name: str, payload: dict[str, Any]) -> None:
        report["steps"].append({"name": name, **payload})

    def check(name: str, condition: bool, detail: Any = None) -> None:
        report["checks"].append(
            {"name": name, "ok": bool(condition), "detail": detail}
        )
        if not condition:
            report["ok"] = False

    source_doc = _load_json(os.path.join(materials, "source.json"))
    pq_segments = source_doc["segments"]
    exercise_specs = _load_jsonl(os.path.join(materials, "exercises.jsonl"))
    double_supply = _load_json(os.path.join(materials, "supply_double.json"))

    # ---- 1. bundled chunk "x y" (single two-segment pair) -----------------
    bundle_content, bundle_path, _bc, bundle_repo = _publish(
        out_dir,
        "PQ_BUNDLE",
        pq_segments,
        _load_jsonl(os.path.join(materials, "translations_bundle.jsonl")),
        "PQBUNDLE",
    )
    check("bundle_package_valid", not validate_package(bundle_content))
    bundle_product = bundle_repo.source.published_product("PQ_BUNDLE")
    bundle_book = analyze_package(bundle_product, read_package(bundle_path))
    check("bundle_one_pair", len(bundle_book.pairs) == 1)
    bundle_space = DictionarySpace(bundle_book)
    check("bundle_consistent", bundle_space.consistent)
    check("bundle_not_unique", not bundle_space.unique)
    check(
        "bundle_p_images_both",
        bundle_space.image_of("p") == ("x", "y"),
        bundle_space.image_of("p"),
    )
    check(
        "bundle_q_images_both",
        bundle_space.image_of("q") == ("x", "y"),
        bundle_space.image_of("q"),
    )
    step(
        "bundle_book",
        {
            "pairs": [p.to_dict() for p in bundle_book.pairs],
            "source_vocab": list(bundle_book.source_vocab),
            "target_vocab": list(bundle_book.target_vocab),
            "dictionaries": bundle_space.all_dictionaries(),
        },
    )

    # ---- 2. tile board on the bundled book --------------------------------
    exercises = [Exercise.from_dict(spec) for spec in exercise_specs]
    supply = package_supply(bundle_book)  # {"x": 1, "y": 1}
    check("bundle_supply_one_each", supply == {"x": 1, "y": 1}, supply)
    bundle_alloc = solve_allocation(bundle_book, bundle_space, exercises, supply)
    step("bundle_allocation", bundle_alloc.to_dict())
    check("bundle_score_3", bundle_alloc.score == 3, bundle_alloc.score)
    check(
        "bundle_only_jia_served",
        [p.table for p in bundle_alloc.plans] == ["甲"],
        [p.table for p in bundle_alloc.plans],
    )
    check(
        "bundle_jia_needs_both_kinds",
        dict(bundle_alloc.plans[0].bag) == {"x": 1, "y": 1},
        bundle_alloc.plans[0].bag,
    )
    check("bundle_yi_unselected", bool(bundle_alloc.unselected))
    if bundle_alloc.unselected:
        short = bundle_alloc.unselected[0]
        check(
            "bundle_unselected_reports_shortage",
            bool(short.shortages) and short.table == "乙",
            short.to_dict(),
        )

    # ---- 3. split package: same visible text, fixed dictionary ------------
    split_content, split_path, _sc, split_repo = _publish(
        out_dir,
        "PQ_SPLIT",
        pq_segments,
        _load_jsonl(os.path.join(materials, "translations_split.jsonl")),
        "PQSPLIT",
    )
    check("split_package_valid", not validate_package(split_content))
    split_product = split_repo.source.published_product("PQ_SPLIT")
    split_book = analyze_package(split_product, read_package(split_path))
    split_space = DictionarySpace(split_book)
    check("split_two_pairs", len(split_book.pairs) == 2)
    check("split_unique", split_space.unique)
    check("split_p_images_x", split_space.image_of("p") == ("x",), split_space.image_of("p"))
    check("split_q_images_y", split_space.image_of("q") == ("y",), split_space.image_of("q"))

    split_alloc = solve_allocation(split_book, split_space, exercises, package_supply(split_book))
    step("split_allocation", split_alloc.to_dict())
    check("split_score_5", split_alloc.score == 5, split_alloc.score)
    check(
        "split_both_tables",
        sorted(p.table for p in split_alloc.plans) == ["乙", "甲"],
        [p.table for p in split_alloc.plans],
    )
    check(
        "split_one_kind_each",
        dict(split_alloc.plans[0].bag) == {"x": 1}
        and dict(split_alloc.plans[1].bag) == {"y": 1},
        [dict(p.bag) for p in split_alloc.plans],
    )

    # Same rendered words, different chunking: the split package must not be
    # accepted as a re-packaging of the bundled booklet.
    try:
        verify_package_against_book(bundle_book, read_package(split_path))
        cross_ok = True
    except Exception:
        cross_ok = False
    check("borrowed_chunking_rejected", not cross_ok)

    # ---- 4. keep the bundled package, add a second copy of each kind ------
    double_alloc = solve_allocation(
        bundle_book, bundle_space, exercises, double_supply
    )
    step("double_supply_allocation", double_alloc.to_dict())
    check("double_score_5", double_alloc.score == 5, double_alloc.score)
    check(
        "double_both_tables",
        sorted(p.table for p in double_alloc.plans) == ["乙", "甲"],
        [p.table for p in double_alloc.plans],
    )

    # ---- 5. two requests feasible alone but not together -----------------
    witness = bundle_space.find_joint_contradiction()
    check("joint_contradiction_found", witness is not None, witness)
    if witness is not None:
        first, second = witness
        alone_first = bundle_space.check_pairs([first]).feasible
        alone_second = bundle_space.check_pairs([second]).feasible
        together = bundle_space.check_pairs([first, second]).feasible
        step(
            "joint_contradiction",
            {
                "first": list(first),
                "first_alone": alone_first,
                "second": list(second),
                "second_alone": alone_second,
                "together": together,
            },
        )
        check("first_alone_feasible", alone_first)
        check("second_alone_feasible", alone_second)
        check("together_infeasible", not together)

    # A feasible multi-pair request must return a complete witness dictionary.
    pair_req = bundle_space.check_pairs([("p", "x")])
    check(
        "feasible_request_has_witness",
        pair_req.feasible and pair_req.dictionary is not None,
        pair_req.to_dict(),
    )

    # ---- 6. contradictory booklet refused --------------------------------
    bad_segments = [("S1", 1, "a b"), ("S2", 1, "c d")]
    bad_chunks = [
        {"translation_id": "B1", "source_ids": ["S1"], "source_revisions": [1], "text": "u u"},
        {"translation_id": "B2", "source_ids": ["S2"], "source_revisions": [1], "text": "v w"},
    ]
    bad_book = build_book("BADDOC", "EN", "P", "sha", bad_segments, bad_chunks)
    bad_space = DictionarySpace(bad_book)
    check("contradictory_book_detected", not bad_space.consistent)
    check("contradictory_book_explained", bad_space.contradiction is not None)
    step(
        "contradictory_book",
        {"consistent": bad_space.consistent, "contradiction": bad_space.contradiction},
    )

    report["packages"] = {
        "PQBUNDLE": dict(bundle_content),
        "PQSPLIT": dict(split_content),
    }
    report["book_fingerprints"] = {
        "bundle_pairs": [p.translation_id for p in bundle_book.pairs],
        "split_pairs": [p.translation_id for p in split_book.pairs],
    }
    return report
