"""Pure warranty-clock engine.

The business calendar is a sequence of consecutive integer days; day 0 is the
deal day and the warranty clock starts counting *on* day 0.  A factory outage
is a half-open integer interval ``[start_day, end_day)``: a unit handed in on
day 1 and released on day 3 is absent on days 1 and 2 only.

Rules implemented here (all side-effect free; persistence lives in
``warranty.py``):

* Overlapping outage work orders stop the clock only once -- the union of the
  outage intervals is taken before counting.
* Only days that were still inside the unexpired warranty are paused.  A brand
  new outage registered after the budget ran out cannot revive the cover; the
  tail of a back-dated outage that crossed the old expiry line *is* effective,
  because on those business days the cover had not yet been exhausted.
* A normal return ends cover on its effective return day.

Given a budget of ``warranty_days`` cover-days, the expiry day is the first
integer day that is no longer covered, i.e. a start-of-work on day
``expiry_day - 1`` is the last legal one and ``expiry_day`` is expired.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

# Expiry reasons.
EXPIRED_BUDGET = "WARRANTY_BUDGET_EXHAUSTED"
EXPIRED_RETURN = "RETURNED_EXIT"
ACTIVE = "ACTIVE"


@dataclass(frozen=True)
class Outage:
    """One confirmed factory-return outage, half-open [start_day, end_day)."""

    start_day: int
    end_day: int
    record_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.start_day, int) or isinstance(self.start_day, bool):
            raise TypeError("start_day must be an integer")
        if not isinstance(self.end_day, int) or isinstance(self.end_day, bool):
            raise TypeError("end_day must be an integer")
        if self.start_day >= self.end_day:
            raise ValueError(f"outage interval must be half-open with start < end: {self}")


def merge_intervals(intervals: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Return the union of half-open integer intervals as disjoint intervals."""

    ordered = sorted((s, e) for s, e in intervals if e > s)
    merged: List[Tuple[int, int]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            prev_start, prev_end = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end))
    return merged


def overlap_days(a: Tuple[Optional[int], Optional[int]], b: Tuple[Optional[int], Optional[int]]) -> int:
    """Integer days inside the overlap of two half-open (possibly open-ended) intervals.

    A ``None`` bound means open: ``None`` on the left is negative infinity, on
    the right positive infinity.  At least one finite upper bound is required.
    """

    a_start, a_end = a
    b_start, b_end = b
    starts = [bound for bound in (a_start, b_start) if bound is not None]
    ends = [bound for bound in (a_end, b_end) if bound is not None]
    if not ends:
        raise ValueError("overlap of two open-ended upper bounds is not finite")
    start = max(starts) if starts else min(ends)
    end = min(ends)
    return max(0, end - start)


def clamp_intervals(
    intervals: Sequence[Tuple[int, int]], lower: int, upper: Optional[int]
) -> List[Tuple[int, int]]:
    """Clip every half-open interval into ``[lower, upper)`` (upper may be open)."""

    result: List[Tuple[int, int]] = []
    for start, end in intervals:
        clipped_start = max(start, lower)
        clipped_end = end if upper is None else min(end, upper)
        if clipped_end > clipped_start:
            result.append((clipped_start, clipped_end))
    return result


def compute_warranty(
    *,
    sold_day: int,
    warranty_days: int,
    outages: Sequence[Outage],
    return_day: Optional[int] = None,
) -> Dict[str, object]:
    """Walk the calendar day by day and count paused vs. consumed cover days.

    The walk is deliberately simple and auditable rather than a closed-form
    formula, because back-dated outages and outages that cross the moving
    expiry boundary interact.  Each day from the sale day is classified once:

    * a day inside the union of outage intervals pauses the clock and does not
      consume the budget;
    * every other day consumes one cover day.

    The walk ends on the first non-paused day on which the budget reaches zero
    -- that day is the expiry day (the first uncovered day).  Outage days
    later than that day are provably ineffectual and reported separately, which
    is exactly the "a post-expiry outage cannot revive the unit" rule.
    """

    if warranty_days < 0:
        raise ValueError("warranty_days must be non-negative")
    intervals = merge_intervals([(o.start_day, o.end_day) for o in outages])

    # Outages before the sale cannot stop a clock that has not started.
    effective_intervals = clamp_intervals(intervals, sold_day, None)
    paused_days = 0
    remaining = warranty_days
    day = sold_day
    expiry_day: Optional[int] = None

    def is_paused(calendar_day: int) -> bool:
        return any(s <= calendar_day < e for s, e in effective_intervals)

    while remaining > 0:
        if is_paused(day):
            paused_days += 1
        else:
            remaining -= 1
            if remaining == 0:
                expiry_day = day + 1
                break
        day += 1

    if expiry_day is None:
        # Zero-day budget: the unit is already expired at the sale day.
        expiry_day = sold_day

    # Independent interval-based accounting, asserted against the day walk.
    effective_paused = sum(
        overlap_days((s, e), (sold_day, expiry_day)) for s, e in effective_intervals
    )
    assert effective_paused == paused_days
    # Outage days on/after the computed expiry hit an already-dead budget.
    ineffectual = sum(
        overlap_days((s, e), (expiry_day, None)) for s, e in effective_intervals
    )

    end_reason = EXPIRED_BUDGET
    cover_until: Optional[int] = expiry_day
    latest_start_day: Optional[int] = expiry_day - 1
    if warranty_days == 0:
        latest_start_day = None
    if return_day is not None:
        # A normal return removes the physical unit from after-sales cover on
        # the return's effective day.  Cover ends on whichever comes first.
        if return_day < expiry_day:
            cover_until = return_day
            latest_start_day = return_day - 1
            end_reason = EXPIRED_RETURN

    return {
        "sold_day": sold_day,
        "warranty_days": warranty_days,
        "outage_intervals": effective_intervals,
        "paused_days": paused_days,
        "ineffectual_outage_days": ineffectual,
        "expiry_day": expiry_day,
        "cover_until_day": cover_until,
        "latest_start_day": latest_start_day,
        "end_reason": end_reason,
    }


def eligibility(summary: Mapping[str, object], query_day: int) -> Dict[str, object]:
    """Answer a service-desk query for one unit on one business day."""

    sold_day = summary["sold_day"]
    cover_until = summary["cover_until_day"]
    latest_start = summary["latest_start_day"]
    end_reason = summary["end_reason"]
    if query_day < sold_day:
        covered = False
        reason = "BEFORE_SALE"
    elif latest_start is not None and query_day <= latest_start:
        covered = True
        reason = ACTIVE
    else:
        covered = False
        reason = end_reason
    return {
        "query_day": query_day,
        "covered": covered,
        "reason": reason,
        "sold_day": sold_day,
        "expiry_day": summary["expiry_day"],
        "cover_until_day": cover_until,
        "latest_start_day": latest_start,
        "paused_days": summary["paused_days"],
        "ineffectual_outage_days": summary["ineffectual_outage_days"],
        "end_reason": end_reason,
    }


def explain_outages(outages: Sequence[Outage], sold_day: int, expiry_day: int) -> List[Dict[str, int]]:
    """Per-record view used by the service desk: which days counted and why."""

    rows: List[Dict[str, object]] = []
    for outage in sorted(outages, key=lambda o: (o.start_day, o.end_day, o.record_id or "")):
        interval = (outage.start_day, outage.end_day)
        rows.append(
            {
                "record_id": outage.record_id,
                "start_day": outage.start_day,
                "end_day": outage.end_day,
                "effective_paused_days": overlap_days(interval, (sold_day, expiry_day)),
                "post_expiry_days": overlap_days(interval, (expiry_day, None)),
                "pre_sale_days": max(0, min(outage.end_day, sold_day) - outage.start_day),
            }
        )
    return rows
