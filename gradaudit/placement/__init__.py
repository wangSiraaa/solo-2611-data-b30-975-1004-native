"""Graduation-internship placement layer on top of gradaudit.

Two sides, independently verifiable:

* Department side -- :func:`review` decides the *joint* post/graduation
  eligibility for one student and one post; :func:`verify_review` re-checks a
  review decision without importing the decision logic, and its embedded
  graduation plan is an ordinary ``course-allocation/1`` document accepted by
  the original verifier.
* Station side -- :func:`solve_matching` searches all stable rosters and
  returns the one placing the most students at minimum total commute subsidy;
  :func:`verify_roster` independently re-derives quotas, approvals, blocking
  pairs and the optimum.
"""

from .documents import (
    MAX_STATION_PICKS,
    MAX_STATIONS,
    MAX_STUDENTS,
    Post,
    Station,
    parse_posts,
    parse_preferences,
    parse_review,
    parse_roster,
    parse_stations,
    parse_subsidies,
)
from .eligibility import ReviewResult, evaluate_eligibility, qualifying_courses
from .matching import (
    MatchingInput,
    RosterResult,
    build_matching_input,
    find_blocking_pairs,
    solve_matching,
)
from .pool import ReviewPool, StudentLibrary
from .review_verify import ReviewVerifyResult, graduation_feasible, verify_review
from .roster_verify import RosterVerifyResult, verify_roster

__all__ = [
    # contracts
    "Post", "Station", "parse_posts", "parse_stations", "parse_preferences",
    "parse_subsidies", "parse_review", "parse_roster",
    "MAX_STUDENTS", "MAX_STATIONS", "MAX_STATION_PICKS",
    # department side
    "evaluate_eligibility", "qualifying_courses", "ReviewResult",
    "verify_review", "ReviewVerifyResult", "graduation_feasible",
    # station side
    "MatchingInput", "RosterResult", "build_matching_input",
    "solve_matching", "find_blocking_pairs",
    "verify_roster", "RosterVerifyResult",
    # pooling
    "ReviewPool", "StudentLibrary",
]
