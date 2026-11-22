"""Public JSON contracts for the graduation-internship placement layer.

Four new document formats sit beside the original gradaudit contracts; the
original four documents (catalog / records / requirements / allocation plan)
are reused unchanged.  The graduation plan embedded in an eligibility review is
an ordinary ``course-allocation/1`` document, so the *old* verifier entry point
accepts it as-is.

* ``internship-posts/1``        -- posts: accepted course categories + points
* ``internship-review/1``       -- department decision for one student x one post
* ``station-profile/1``         -- stations: quota, strict reception order, closed flag
* ``student-preferences/1``     -- each student's strict wish order over stations
* ``commute-subsidies/1``       -- subsidy per (station, student) pair
* ``internship-roster/1``       -- the signed matching roster + blocking-pair report

As with the catalog contract, nothing here guesses: a pair is admissible only
when both sides explicitly list each other *and* a department review approved
it.  Money amounts on a roster are recomputed from the subsidy document, never
trusted from the roster itself -- the same discipline as "points come from the
record, never from the plan".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Optional, Tuple

from ..model import _as_positive_int, _require

# Placement scale contract (the prompt's hard limits).
MAX_STUDENTS = 24
MAX_STATIONS = 10
MAX_STATION_PICKS = 5

POSTS_FORMAT = "internship-posts/1"
REVIEW_FORMAT = "internship-review/1"
STATION_FORMAT = "station-profile/1"
PREFERENCES_FORMAT = "student-preferences/1"
SUBSIDY_FORMAT = "commute-subsidies/1"
ROSTER_FORMAT = "internship-roster/1"

# Stable machine-readable reason codes shared by the eligibility decision and
# its *independent* verifier.  They are part of the document contract, so they
# live next to the formats; neither side imports the other's logic.
GRADUATION_NOT_FEASIBLE = "graduation_not_feasible"
POST_REQUIREMENTS_UNMET = "post_requirements_unmet"
GRADUATION_BLOCKED_BY_RESERVATION = "graduation_blocked_by_reservation"


# --------------------------------------------------------------------------- #
# posts
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Post:
    post_id: str
    accepted_categories: FrozenSet[str]
    required_points: int


def parse_posts(doc: Any) -> Dict[str, Post]:
    _require(isinstance(doc, dict), "posts document must be a JSON object")
    _require(doc.get("format") == POSTS_FORMAT, f"format must be {POSTS_FORMAT!r}", location="format")
    raw = doc.get("posts")
    _require(isinstance(raw, list) and raw, "posts must be a non-empty list", location="posts")
    posts: Dict[str, Post] = {}
    for idx, entry in enumerate(raw):
        loc = f"posts[{idx}]"
        _require(isinstance(entry, dict), "post must be an object", location=loc)
        pid = entry.get("post_id")
        _require(isinstance(pid, str) and pid.strip() != "", "post_id must be a non-empty string", location=loc)
        _require(pid not in posts, f"duplicate post_id {pid!r}", location=pid)
        cats = entry.get("accepted_categories")
        _require(isinstance(cats, list) and cats, "accepted_categories must be a non-empty list", location=pid)
        norm = set()
        for cat in cats:
            _require(isinstance(cat, str) and cat.strip() != "", "category must be a non-empty string", location=pid)
            norm.add(cat)
        minimum = _as_positive_int(entry.get("required_points"), field_name="required_points", location=pid)
        posts[pid] = Post(post_id=pid, accepted_categories=frozenset(norm), required_points=minimum)
    return posts


# --------------------------------------------------------------------------- #
# station profiles
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Station:
    station_id: str
    quota: int
    ranking: Tuple[str, ...]          # strict reception order, best first
    closed: bool


def parse_stations(doc: Any) -> Dict[str, Station]:
    _require(isinstance(doc, dict), "station profile must be a JSON object")
    _require(doc.get("format") == STATION_FORMAT, f"format must be {STATION_FORMAT!r}", location="format")
    raw = doc.get("stations")
    _require(isinstance(raw, list) and raw, "stations must be a non-empty list", location="stations")
    _require(len(raw) <= MAX_STATIONS, f"at most {MAX_STATIONS} stations are supported", location="stations")
    stations: Dict[str, Station] = {}
    for idx, entry in enumerate(raw):
        loc = f"stations[{idx}]"
        _require(isinstance(entry, dict), "station must be an object", location=loc)
        sid = entry.get("station_id")
        _require(isinstance(sid, str) and sid.strip() != "", "station_id must be a non-empty string", location=loc)
        _require(sid not in stations, f"duplicate station_id {sid!r}", location=sid)
        quota = entry.get("quota")
        _require(isinstance(quota, int) and not isinstance(quota, bool) and quota >= 0,
                 "quota must be a non-negative integer", location=sid)
        ranking = entry.get("ranking")
        _require(isinstance(ranking, list) and ranking, "ranking must be a non-empty list (strict order)", location=sid)
        seen = set()
        for student in ranking:
            _require(isinstance(student, str) and student.strip() != "",
                     "ranking entries must be non-empty strings", location=sid)
            _require(student not in seen, f"station {sid!r} ranks {student!r} more than once (order must be strict)",
                     location=sid)
            seen.add(student)
        closed = bool(entry.get("closed", False))
        stations[sid] = Station(station_id=sid, quota=quota, ranking=tuple(ranking), closed=closed)
    return stations


# --------------------------------------------------------------------------- #
# student preferences
# --------------------------------------------------------------------------- #
def parse_preferences(doc: Any, *, known_students: Optional[FrozenSet[str]] = None,
                      known_stations: Optional[FrozenSet[str]] = None) -> Dict[str, Tuple[str, ...]]:
    _require(isinstance(doc, dict), "preferences document must be a JSON object")
    _require(doc.get("format") == PREFERENCES_FORMAT, f"format must be {PREFERENCES_FORMAT!r}", location="format")
    raw = doc.get("preferences")
    _require(isinstance(raw, list) and raw, "preferences must be a non-empty list", location="preferences")
    _require(len(raw) <= MAX_STUDENTS, f"at most {MAX_STUDENTS} students are supported", location="preferences")
    prefs: Dict[str, Tuple[str, ...]] = {}
    for idx, entry in enumerate(raw):
        loc = f"preferences[{idx}]"
        _require(isinstance(entry, dict), "preference entry must be an object", location=loc)
        sid = entry.get("student_id")
        _require(isinstance(sid, str) and sid.strip() != "", "student_id must be a non-empty string", location=loc)
        _require(sid not in prefs, f"duplicate preferences for student {sid!r}", location=sid)
        if known_students is not None:
            _require(sid in known_students, f"unknown student {sid!r}", location=sid)
        stations = entry.get("stations")
        _require(isinstance(stations, list) and stations, "stations must be a non-empty list", location=sid)
        _require(len(stations) <= MAX_STATION_PICKS,
                 f"a student may list at most {MAX_STATION_PICKS} stations (got {len(stations)})", location=sid)
        seen = set()
        for t in stations:
            _require(isinstance(t, str) and t.strip() != "", "station id must be a non-empty string", location=sid)
            _require(t not in seen, f"student {sid!r} lists station {t!r} more than once (order must be strict)",
                     location=sid)
            if known_stations is not None:
                _require(t in known_stations, f"student {sid!r} prefers unknown station {t!r}", location=sid)
            seen.add(t)
        prefs[sid] = tuple(stations)
    return prefs


# --------------------------------------------------------------------------- #
# commute subsidies
# --------------------------------------------------------------------------- #
def parse_subsidies(doc: Any, *, known_stations: Optional[FrozenSet[str]] = None
                    ) -> Dict[str, Dict[str, int]]:
    _require(isinstance(doc, dict), "subsidies document must be a JSON object")
    _require(doc.get("format") == SUBSIDY_FORMAT, f"format must be {SUBSIDY_FORMAT!r}", location="format")
    raw = doc.get("subsidies")
    _require(isinstance(raw, list) and raw, "subsidies must be a non-empty list", location="subsidies")
    out: Dict[str, Dict[str, int]] = {}
    for idx, entry in enumerate(raw):
        loc = f"subsidies[{idx}]"
        _require(isinstance(entry, dict), "subsidy entry must be an object", location=loc)
        sid = entry.get("station_id")
        _require(isinstance(sid, str) and sid.strip() != "", "station_id must be a non-empty string", location=loc)
        _require(sid not in out, f"duplicate subsidy table for station {sid!r}", location=sid)
        if known_stations is not None:
            _require(sid in known_stations, f"subsidy for unknown station {sid!r}", location=sid)
        amounts = entry.get("amounts")
        _require(isinstance(amounts, dict) and amounts, "amounts must be a non-empty object", location=sid)
        table: Dict[str, int] = {}
        for student, amount in amounts.items():
            _require(isinstance(student, str) and student.strip() != "",
                     "student id must be a non-empty string", location=sid)
            _require(isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0,
                     f"subsidy for {student!r} must be a non-negative integer", location=sid)
            table[student] = amount
        out[sid] = table
    return out


# --------------------------------------------------------------------------- #
# review document (structural parse; semantics belong to review_verify)
# --------------------------------------------------------------------------- #
def parse_review(doc: Any) -> dict:
    _require(isinstance(doc, dict), "review document must be a JSON object")
    _require(doc.get("format") == REVIEW_FORMAT, f"format must be {REVIEW_FORMAT!r}", location="format")
    for key in ("student_id", "post_id", "decision", "request_id"):
        val = doc.get(key)
        _require(isinstance(val, str) and val.strip() != "", f"{key} must be a non-empty string", location=key)
    _require(doc["decision"] in ("eligible", "ineligible"),
             "decision must be 'eligible' or 'ineligible'", location="decision")
    for key in ("catalog_revision", "records_revision", "requirements_revision"):
        val = doc.get(key)
        _require(isinstance(val, int) and not isinstance(val, bool) and val >= 1,
                 f"{key} must be a positive integer", location=key)
    post = doc.get("post")
    _require(isinstance(post, dict), "review must embed the post it judged", location="post")
    reservation = doc.get("reservation", [])
    _require(isinstance(reservation, list), "reservation must be a list", location="reservation")
    for idx, entry in enumerate(reservation):
        _require(isinstance(entry, dict), "reservation entry must be an object", location=f"reservation[{idx}]")
        for key in ("record_id", "course_id"):
            val = entry.get(key)
            _require(isinstance(val, str) and val.strip() != "", f"reservation {key} must be a non-empty string",
                     location=f"reservation[{idx}]")
    reasons = doc.get("reasons", [])
    _require(isinstance(reasons, list), "reasons must be a list", location="reasons")
    return doc


# --------------------------------------------------------------------------- #
# roster document (structural parse; semantics belong to roster_verify)
# --------------------------------------------------------------------------- #
def parse_roster(doc: Any) -> dict:
    _require(isinstance(doc, dict), "roster document must be a JSON object")
    _require(doc.get("format") == ROSTER_FORMAT, f"format must be {ROSTER_FORMAT!r}", location="format")
    assignments = doc.get("assignments")
    _require(isinstance(assignments, list), "assignments must be a list", location="assignments")
    for idx, entry in enumerate(assignments):
        loc = f"assignments[{idx}]"
        _require(isinstance(entry, dict), "assignment must be an object", location=loc)
        for key in ("student_id", "station_id"):
            val = entry.get(key)
            _require(isinstance(val, str) and val.strip() != "", f"{key} must be a non-empty string", location=loc)
    unassigned = doc.get("unassigned")
    _require(isinstance(unassigned, list), "unassigned must be a list", location="unassigned")
    admissions = doc.get("admissions")
    _require(isinstance(admissions, list), "admissions must be a list", location="admissions")
    return doc
