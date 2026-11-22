"""Command line interface for gradaudit.

Two groups of commands:

Stateless (operate on JSON files directly)
    verify     -- independent verify-plan entry point (never calls the solver)
    solve      -- exact feasibility solver, prints a course-allocation/1 plan
    candidates -- list a transfer course's candidate local courses + categories

Persistent (SQLite-backed catalog / record / audit library)
    import-catalog / import-records / import-requirements
    prepare / confirm / status / reopen / history / list-*
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional

from .errors import GradAuditError
from .library import Store
from .model import parse_catalog, parse_records, parse_requirements
from .placement import (
    build_matching_input,
    evaluate_eligibility,
    parse_posts,
    parse_preferences,
    parse_review,
    parse_stations,
    parse_subsidies,
    solve_matching,
    verify_review,
    verify_roster,
)
from .solver import solve
from .verify import verify_plan_document


def _load(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _dump(obj: Any) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=False)
    sys.stdout.write("\n")


def _inputs(args) -> tuple:
    catalog = parse_catalog(_load(args.catalog))
    records = parse_records(_load(args.records))
    requirements = parse_requirements(_load(args.requirements))
    return catalog, records, requirements


# --------------------------------------------------------------------------- #
# stateless commands
# --------------------------------------------------------------------------- #
def cmd_verify(args) -> int:
    catalog, records, requirements = _inputs(args)
    plan_doc = _load(args.plan)
    result = verify_plan_document(plan_doc, catalog, records, requirements)
    _dump(result.as_dict())
    return 0 if result.accepted else 1


def cmd_solve(args) -> int:
    catalog, records, requirements = _inputs(args)
    result = solve(catalog, records, requirements)
    _dump(result.as_dict())
    if args.emit_plan and result.feasible:
        # write the plan next to the summary when requested
        with open(args.emit_plan, "w", encoding="utf-8") as fh:
            json.dump(result.as_plan_document(), fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    return 0 if result.feasible else 1


def cmd_candidates(args) -> int:
    catalog = parse_catalog(_load(args.catalog))
    _dump({
        "catalog_revision": catalog.revision,
        "external_course": args.external_course,
        "candidates": catalog.candidates_with_categories(args.external_course),
    })
    return 0


# --------------------------------------------------------------------------- #
# persistent commands
# --------------------------------------------------------------------------- #
def _store(args) -> Store:
    return Store(args.db)


def cmd_import_catalog(args) -> int:
    with _store(args) as store:
        catalog = store.import_catalog(_load(args.file))
        _dump({"imported": "catalog", "revision": catalog.revision, "content_hash": catalog.content_hash()})
    return 0


def cmd_import_records(args) -> int:
    with _store(args) as store:
        records = store.import_records(_load(args.file))
        _dump({"imported": "records", "revision": records.revision, "student_id": records.student_id,
               "content_hash": records.content_hash()})
    return 0


def cmd_import_requirements(args) -> int:
    with _store(args) as store:
        req = store.import_requirements(_load(args.file))
        _dump({"imported": "requirements", "revision": req.revision, "request_id": req.request_id,
               "content_hash": req.content_hash()})
    return 0


def cmd_list_candidates(args) -> int:
    with _store(args) as store:
        _dump({"catalog_revision": args.catalog_revision,
               "external_course": args.external_course,
               "candidates": store.list_candidates(args.catalog_revision, args.external_course)})
    return 0


def cmd_prepare(args) -> int:
    with _store(args) as store:
        plan_doc = _load(args.plan)
        result = store.prepare_audit(
            plan_doc,
            request_id=args.request,
            catalog_revision=args.catalog_revision,
            records_revision=args.records_revision,
            requirements_revision=args.requirements_revision,
        )
        _dump(result)
        return 0 if result["accepted"] else 1


def cmd_confirm(args) -> int:
    with _store(args) as store:
        _dump(store.confirm_audit(request_id=args.request, preparation_id=args.preparation))
    return 0


def cmd_status(args) -> int:
    with _store(args) as store:
        _dump(store.confirmation_status(args.request))
    return 0


def cmd_reopen(args) -> int:
    with _store(args) as store:
        _dump(store.reopen_audit(request_id=args.request, reason=args.reason))
    return 0


def cmd_history(args) -> int:
    with _store(args) as store:
        _dump({
            "request_id": args.request,
            "history": store.history(args.request),
            "preparations": store.list_preparations(args.request),
            "confirmations": store.list_confirmations(args.request),
        })
    return 0


def cmd_points(args) -> int:
    with _store(args) as store:
        _dump({"preparation_id": args.preparation,
               "points": store.preparation_points(args.preparation)})
    return 0


# --------------------------------------------------------------------------- #
# placement commands (internship layer)
# --------------------------------------------------------------------------- #
def cmd_placement_review(args) -> int:
    catalog, records, requirements = _inputs(args)
    posts = parse_posts(_load(args.posts))
    if args.post not in posts:
        _dump({"error": {"code": "unknown_post", "message": f"post {args.post!r} is not defined"}})
        return 2
    result = evaluate_eligibility(catalog, records, requirements, posts[args.post])
    doc = result.as_document()
    _dump(doc)
    if args.emit_review:
        with open(args.emit_review, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    return 0 if result.decision == "eligible" else 1


def cmd_placement_verify_review(args) -> int:
    catalog, records, requirements = _inputs(args)
    posts = parse_posts(_load(args.posts))
    result = verify_review(_load(args.review), catalog, records, requirements, posts)
    _dump(result.as_dict())
    return 0 if result.accepted else 1


def cmd_placement_match(args) -> int:
    stations = parse_stations(_load(args.stations))
    preferences = parse_preferences(
        _load(args.preferences),
        known_students=None,
        known_stations=frozenset(stations),
    )
    subsidies = parse_subsidies(_load(args.subsidies), known_stations=frozenset(stations))
    reviews = [parse_review(_load(path)) for path in args.reviews]
    inp = build_matching_input(preferences, stations, reviews, subsidies)
    result = solve_matching(inp)
    doc = result.as_document(inp)
    _dump(doc)
    if args.emit_roster:
        with open(args.emit_roster, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    return 0 if result.stable else 1


def cmd_placement_verify_roster(args) -> int:
    stations = parse_stations(_load(args.stations))
    preferences = parse_preferences(
        _load(args.preferences),
        known_students=None,
        known_stations=frozenset(stations),
    )
    subsidies = parse_subsidies(_load(args.subsidies), known_stations=frozenset(stations))
    reviews = [parse_review(_load(path)) for path in args.reviews]
    result = verify_roster(_load(args.roster), preferences, stations, subsidies, reviews)
    _dump(result.as_dict())
    return 0 if result.accepted else 1


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gradaudit", description="Course-equivalence catalog and graduation audit library")
    sub = p.add_subparsers(dest="command", required=True)

    def add_inputs(sp):
        sp.add_argument("--catalog", required=True, help="course-catalog/1 JSON file")
        sp.add_argument("--records", required=True, help="student records JSON file")
        sp.add_argument("--requirements", required=True, help="requirements JSON file")

    # verify
    sp = sub.add_parser("verify", help="independently verify a course-allocation/1 plan")
    sp.add_argument("--plan", required=True)
    add_inputs(sp)
    sp.set_defaults(func=cmd_verify)

    # solve
    sp = sub.add_parser("solve", help="exactly solve feasibility and emit an allocation")
    add_inputs(sp)
    sp.add_argument("--emit-plan", help="optional path to write a course-allocation/1 plan")
    sp.set_defaults(func=cmd_solve)

    # candidates (stateless)
    sp = sub.add_parser("candidates", help="list candidate local courses for an external course")
    sp.add_argument("external_course")
    sp.add_argument("--catalog", required=True)
    sp.set_defaults(func=cmd_candidates)

    # imports
    sp = sub.add_parser("import-catalog", help="import a catalog revision into the library")
    sp.add_argument("file")
    sp.add_argument("--db", default="gradaudit.sqlite3")
    sp.set_defaults(func=cmd_import_catalog)

    sp = sub.add_parser("import-records", help="import a student-records revision")
    sp.add_argument("file")
    sp.add_argument("--db", default="gradaudit.sqlite3")
    sp.set_defaults(func=cmd_import_records)

    sp = sub.add_parser("import-requirements", help="import a requirements revision")
    sp.add_argument("file")
    sp.add_argument("--db", default="gradaudit.sqlite3")
    sp.set_defaults(func=cmd_import_requirements)

    sp = sub.add_parser("list-candidates", help="list candidates from the library catalog")
    sp.add_argument("catalog_revision", type=int)
    sp.add_argument("external_course")
    sp.add_argument("--db", default="gradaudit.sqlite3")
    sp.set_defaults(func=cmd_list_candidates)

    # audit lifecycle
    sp = sub.add_parser("prepare", help="independently verify a plan and persist a preparation")
    sp.add_argument("--plan", required=True)
    sp.add_argument("--request", required=True)
    sp.add_argument("--catalog-revision", type=int, required=True)
    sp.add_argument("--records-revision", type=int, required=True)
    sp.add_argument("--requirements-revision", type=int, required=True)
    sp.add_argument("--db", default="gradaudit.sqlite3")
    sp.set_defaults(func=cmd_prepare)

    sp = sub.add_parser("confirm", help="confirm the latest (or a given) accepted preparation")
    sp.add_argument("--request", required=True)
    sp.add_argument("--preparation", default=None)
    sp.add_argument("--db", default="gradaudit.sqlite3")
    sp.set_defaults(func=cmd_confirm)

    sp = sub.add_parser("status", help="show confirmation status (including staleness)")
    sp.add_argument("--request", required=True)
    sp.add_argument("--db", default="gradaudit.sqlite3")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("reopen", help="explicitly reopen a confirmed audit")
    sp.add_argument("--request", required=True)
    sp.add_argument("--reason", required=True)
    sp.add_argument("--db", default="gradaudit.sqlite3")
    sp.set_defaults(func=cmd_reopen)

    sp = sub.add_parser("history", help="show full audit history for a request")
    sp.add_argument("--request", required=True)
    sp.add_argument("--db", default="gradaudit.sqlite3")
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("points", help="show the point destinations stored for a preparation")
    sp.add_argument("preparation_id")
    sp.add_argument("--db", default="gradaudit.sqlite3")
    sp.set_defaults(func=cmd_points)

    # ------------------------------------------------------------------ #
    # internship placement
    # ------------------------------------------------------------------ #
    sp = sub.add_parser("placement-review",
                        help="joint post/graduation eligibility review for one student and one post")
    add_inputs(sp)
    sp.add_argument("--posts", required=True, help="internship-posts/1 JSON file")
    sp.add_argument("--post", required=True, help="post_id to review")
    sp.add_argument("--emit-review", help="optional path to write the internship-review/1 document")
    sp.set_defaults(func=cmd_placement_review)

    sp = sub.add_parser("placement-verify-review",
                        help="independently verify an internship-review/1 decision")
    add_inputs(sp)
    sp.add_argument("--posts", required=True)
    sp.add_argument("--review", required=True)
    sp.set_defaults(func=cmd_placement_verify_review)

    sp = sub.add_parser("placement-match",
                        help="exact stable matching: most placements, then minimum subsidy")
    sp.add_argument("--stations", required=True, help="station-profile/1 JSON file")
    sp.add_argument("--preferences", required=True, help="student-preferences/1 JSON file")
    sp.add_argument("--subsidies", required=True, help="commute-subsidies/1 JSON file")
    sp.add_argument("--reviews", required=True, nargs="+",
                    help="one or more internship-review/1 JSON files")
    sp.add_argument("--emit-roster", help="optional path to write the internship-roster/1 document")
    sp.set_defaults(func=cmd_placement_match)

    sp = sub.add_parser("placement-verify-roster",
                        help="independently verify an internship-roster/1 roster and its optimum")
    sp.add_argument("--roster", required=True)
    sp.add_argument("--stations", required=True)
    sp.add_argument("--preferences", required=True)
    sp.add_argument("--subsidies", required=True)
    sp.add_argument("--reviews", required=True, nargs="+")
    sp.set_defaults(func=cmd_placement_verify_roster)

    return p


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except GradAuditError as exc:
        _dump({"error": exc.as_reason()})
        return 2
    except FileNotFoundError as exc:
        _dump({"error": {"code": "file_not_found", "message": str(exc)}})
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
