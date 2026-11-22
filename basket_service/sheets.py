"""Physical double-sided coupon sheets: geometry, perforations and tearing.

A *sheet* is a small rectangular grid of cells (at most 6x6).  Tickets are
printed on one of two faces; every ticket occupies a *target rectangle* in the
same paper coordinate system used by both faces::

    rect = (row0, col0, row1, col1)     # inclusive cell coordinates

A ticket is redeemable only while its whole target rectangle is one intact
piece of paper.  Taking such a piece hands over *both faces* of it.

Unit perforations live on the shared edges between adjacent cells::

    ("V", r, c)   vertical edge between (r, c) and (r, c + 1)
    ("H", r, c)   horizontal edge between (r, c) and (r + 1, c)

Tearing model:

* a piece is always a rectangle;
* one operation splits one existing rectangle along a single horizontal or
  vertical line, edge to edge of that piece;
* every unit perforation the line runs along inside that piece must be
  punched.  A vertical cut at column boundary ``k`` over rows ``r0..r1``
  needs ``("V", r, k)`` for every row in that span; a horizontal cut at row
  boundary ``k`` over columns ``c0..c1`` needs ``("H", k, c)`` for every
  column;
* unused pieces/corners may be discarded (they stay as physical remainder).

The craft record stores punched edges; missing holes may be corrected at
receipt time (corrections only add holes, never remove one).  This module is
pure: trials never mutate anything.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from .errors import INVALID_PERFORATION, INVALID_SHEET, INVALID_TICKET, BasketError
from .validation import is_int, require_id

Rect = Tuple[int, int, int, int]
Edge = Tuple[str, int, int]

MAX_ROWS = 6
MAX_COLS = 6
MAX_TICKETS = 12          # both faces together
MAX_SHEETS_PER_EVENT = 12
SIDES = ("front", "back")


# ---------------------------------------------------------------------------
# rectangles
# ---------------------------------------------------------------------------


def rect_cells(rect: Rect) -> FrozenSet[Tuple[int, int]]:
    r0, c0, r1, c1 = rect
    return frozenset((r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1))


def rect_area(rect: Rect) -> int:
    r0, c0, r1, c1 = rect
    return (r1 - r0 + 1) * (c1 - c0 + 1)


def rect_contains(outer: Rect, inner: Rect) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def rects_disjoint(a: Rect, b: Rect) -> bool:
    return a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1]


def rect_intersection(a: Rect, b: Rect) -> Optional[Rect]:
    r0, c0 = max(a[0], b[0]), max(a[1], b[1])
    r1, c1 = min(a[2], b[2]), min(a[3], b[3])
    if r0 > r1 or c0 > c1:
        return None
    return (r0, c0, r1, c1)


def whole_sheet_rect(rows: int, cols: int) -> Rect:
    return (0, 0, rows - 1, cols - 1)


def rect_key(rect: Rect) -> Tuple[int, int, int, int]:
    return tuple(rect)  # type: ignore[return-value]


def split_rect(rect: Rect, kind: str, at: int) -> Tuple[Rect, Rect]:
    r0, c0, r1, c1 = rect
    if kind == "H":
        return (r0, c0, at, c1), (at + 1, c0, r1, c1)
    return (r0, c0, r1, at), (r0, at + 1, r1, c1)


def cut_edges(rect: Rect, kind: str, at: int) -> List[Edge]:
    """Unit edges a full cut of ``rect`` must run along."""

    r0, c0, r1, c1 = rect
    if kind == "H":
        return [("H", at, c) for c in range(c0, c1 + 1)]
    return [("V", r, at) for r in range(r0, r1 + 1)]


# ---------------------------------------------------------------------------
# canonicalisation
# ---------------------------------------------------------------------------


def canonical_rect(raw: Any, rows: int, cols: int, sheet_id: str, ticket_id: str) -> Rect:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence) or len(raw) != 4:
        raise BasketError(INVALID_TICKET, "rect must be [row0, col0, row1, col1]", {"sheet_id": sheet_id, "ticket_id": ticket_id})
    if not all(is_int(v) for v in raw):
        raise BasketError(INVALID_TICKET, "rect coordinates must be integers", {"sheet_id": sheet_id, "ticket_id": ticket_id})
    r0, c0, r1, c1 = (int(v) for v in raw)
    if not (0 <= r0 <= r1 < rows and 0 <= c0 <= c1 < cols):
        raise BasketError(INVALID_TICKET, "ticket rectangle is not inside the sheet", {"sheet_id": sheet_id, "ticket_id": ticket_id})
    return (r0, c0, r1, c1)


def parse_edge(value: Any, rows: int, cols: int, sheet_id: str, index: int) -> Edge:
    if isinstance(value, str):
        text = value.strip()
        if len(text) < 3:
            raise BasketError(INVALID_PERFORATION, "malformed perforation", {"sheet_id": sheet_id, "value": value})
        kind = text[0].upper()
        try:
            a_text, b_text = text[1:].split(",")
            a, b = int(a_text), int(b_text)
        except (ValueError, AttributeError):
            raise BasketError(INVALID_PERFORATION, "malformed perforation", {"sheet_id": sheet_id, "value": value})
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 3:
        kind, a, b = value
        if not isinstance(kind, str) or not is_int(a) or not is_int(b):
            raise BasketError(INVALID_PERFORATION, "perforation must be (kind, a, b) with integer coordinates", {"sheet_id": sheet_id})
        kind = kind.upper()
    else:
        raise BasketError(INVALID_PERFORATION, "perforation must be 'Hr,c'/'Vr,c' or (kind, a, b)", {"sheet_id": sheet_id})
    if kind == "H":
        if not (0 <= a < rows - 1 and 0 <= b < cols):
            raise BasketError(INVALID_PERFORATION, "horizontal perforation out of bounds", {"sheet_id": sheet_id, "value": value})
    elif kind == "V":
        if not (0 <= a < rows and 0 <= b < cols - 1):
            raise BasketError(INVALID_PERFORATION, "vertical perforation out of bounds", {"sheet_id": sheet_id, "value": value})
    else:
        raise BasketError(INVALID_PERFORATION, "perforation kind must be H or V", {"sheet_id": sheet_id, "value": value})
    return (kind, int(a), int(b))


def edge_str(edge: Edge) -> str:
    return f"{edge[0]}{edge[1]},{edge[2]}"


def canonical_sheet(sheet: Any, *, keep_id: bool = True) -> Dict[str, Any]:
    if not isinstance(sheet, Mapping):
        raise BasketError(INVALID_SHEET, "sheet must be an object")
    if keep_id:
        sheet_id = require_id(sheet.get("sheet_id"), INVALID_SHEET, "sheet_id")
    else:
        sheet_id = str(sheet.get("sheet_id", ""))
        if not sheet_id:
            raise BasketError(INVALID_SHEET, "sheet_id must be a non-empty string")
    rows, cols = sheet.get("rows"), sheet.get("cols")
    if not is_int(rows) or not 1 <= rows <= MAX_ROWS:
        raise BasketError(INVALID_SHEET, f"rows must be an integer in 1..{MAX_ROWS}", {"sheet_id": sheet_id})
    if not is_int(cols) or not 1 <= cols <= MAX_COLS:
        raise BasketError(INVALID_SHEET, f"cols must be an integer in 1..{MAX_COLS}", {"sheet_id": sheet_id})

    raw_perf = sheet.get("perforations") or []
    if isinstance(raw_perf, (str, bytes)) or not isinstance(raw_perf, Sequence):
        raise BasketError(INVALID_PERFORATION, "perforations must be a list", {"sheet_id": sheet_id})
    perforations = sorted({edge_str(parse_edge(v, rows, cols, sheet_id, i)) for i, v in enumerate(raw_perf)})

    raw_tickets = sheet.get("tickets") or []
    if isinstance(raw_tickets, (str, bytes)) or not isinstance(raw_tickets, Sequence):
        raise BasketError(INVALID_TICKET, "tickets must be a list", {"sheet_id": sheet_id})
    if len(raw_tickets) > MAX_TICKETS:
        raise BasketError(INVALID_TICKET, f"a sheet holds at most {MAX_TICKETS} tickets", {"sheet_id": sheet_id})
    tickets: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_tickets):
        ticket = canonical_ticket(item, rows, cols, sheet_id, index)
        if ticket["ticket_id"] in seen:
            raise BasketError(INVALID_TICKET, "duplicate ticket_id on sheet", {"sheet_id": sheet_id, "ticket_id": ticket["ticket_id"]})
        seen.add(ticket["ticket_id"])
        tickets.append(ticket)
    for side in SIDES:
        face = [t for t in tickets if t["side"] == side]
        for i in range(len(face)):
            for j in range(i + 1, len(face)):
                if rect_intersection(face[i]["rect"], face[j]["rect"]) is not None:
                    raise BasketError(
                        INVALID_TICKET,
                        "tickets on the same face overlap",
                        {"sheet_id": sheet_id, "tickets": [face[i]["ticket_id"], face[j]["ticket_id"]]},
                    )
    tickets.sort(key=lambda t: (t["ticket_id"], t["side"]))
    return {"sheet_id": sheet_id, "rows": rows, "cols": cols, "perforations": perforations, "tickets": tickets}


def canonical_ticket(item: Any, rows: int, cols: int, sheet_id: str, index: int) -> Dict[str, Any]:
    if not isinstance(item, Mapping):
        raise BasketError(INVALID_TICKET, "ticket entries must be objects", {"sheet_id": sheet_id, "index": index})
    ticket_id = require_id(item.get("ticket_id"), INVALID_TICKET, f"tickets[{index}].ticket_id")
    side = item.get("side")
    if side not in SIDES:
        raise BasketError(INVALID_TICKET, "ticket side must be 'front' or 'back'", {"sheet_id": sheet_id, "ticket_id": ticket_id})
    rect = canonical_rect(item.get("rect"), rows, cols, sheet_id, ticket_id)
    coupon_id = require_id(item.get("coupon_id"), INVALID_TICKET, f"tickets[{index}].coupon_id")
    discount = item.get("D")
    if not is_int(discount) or discount < 1:
        raise BasketError(INVALID_TICKET, "ticket D must be a positive integer", {"sheet_id": sheet_id, "ticket_id": ticket_id})
    requires = item.get("requires")
    if not isinstance(requires, Mapping) or len(requires) == 0:
        raise BasketError(INVALID_TICKET, "ticket requires must be a non-empty sku->count mapping", {"sheet_id": sheet_id, "ticket_id": ticket_id})
    canonical_requires: Dict[str, int] = {}
    for sku, count in requires.items():
        if not isinstance(sku, str) or sku == "":
            raise BasketError(INVALID_TICKET, "requires keys must be non-empty sku strings", {"sheet_id": sheet_id, "ticket_id": ticket_id})
        if not is_int(count) or count < 1:
            raise BasketError(INVALID_TICKET, "requires counts must be positive integers", {"sheet_id": sheet_id, "ticket_id": ticket_id})
        canonical_requires[sku] = count
    return {
        "ticket_id": ticket_id,
        "side": side,
        "rect": rect,
        "coupon_id": coupon_id,
        "D": discount,
        "requires": dict(sorted(canonical_requires.items())),
    }


def effective_edges(sheet: Mapping[str, Any], corrections: Iterable[Any] = ()) -> FrozenSet[Edge]:
    """Punched edges after receipt-time corrections (add-only)."""

    rows, cols = sheet["rows"], sheet["cols"]
    edges = {parse_edge(v, rows, cols, sheet["sheet_id"], i) for i, v in enumerate(sheet["perforations"])}
    for index, value in enumerate(corrections):
        edges.add(parse_edge(value, rows, cols, sheet["sheet_id"], index))
    return frozenset(edges)
