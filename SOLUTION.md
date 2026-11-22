# Basket quote service, after-sales ledger and physical coupon paper

A standalone, Python 3.11 solution for the brief in `README.md`:

* an **optimal basket quote service** with immutable published snapshots,
  deterministic two-level tie-breaking and proportional per-unit discount
  allocation;
* an **after-sales ledger** that binds each sale to a complete published quote
  and refunds exactly the per-unit amount that sale paid, with idempotent
  requests, all-or-nothing overlap rejection, and transactional stock moves;
* a **physical double-sided coupon paper** layer: grid sheets with per-edge
  perforations, real tear sequences, per-ticket redemption, and a joint
  optimiser that minimises the combined payment of up to five baskets while
  handing each redeemed ticket to exactly one basket.

No external dependencies are required — only the standard library and SQLite.

## Quick start

```bash
# Full demo: rebuilds data/demo.db and writes results/demo_plan.json +
# results/demo_results.json, including independent cross-checks.
python3 -m basket_service demo

# Unit / differential / concurrency tests (40 tests).
python3 -m basket_service self-check
# or: python3 -m unittest discover -s tests -v

# Run a JSON plan against a real database.
python3 -m basket_service run-plan results/demo_plan.json --db data/plan.db
```

Callable entry point:

```python
from basket_service import open_service

svc = open_service("data/shop.db")

snap = svc.publish_snapshot(products=[...], coupons=[...])   # immutable snapshot
quote = svc.quote(basket, coupons=[], request_id="q1", snapshot_id=snap)
sale = svc.register_sale("S1", quote=quote)                   # binds the full quote
result = svc.return_units("r1", "S1", ["b"], {"b": "RESTOCK"})
svc.adjust_inventory("adj1", "B", 3, "RESTOCK")               # independent stock event

# Physical coupon paper -------------------------------------------------
svc.register_sheet("s1", rows=1, cols=2, perforations=["V0,0"], tickets=[...])
svc.try_tear("s1", ["A", "B"])                                # trial: never moves paper
trial = svc.quote_group("g1", baskets=[...], request_id="qg") # joint optimum
svc.commit_group("qg", {"basket-1": "S1", "basket-2": "S2"})  # moves paper + records sales
```

`register_sale` also accepts `request_id=`/`quote_id=` when the ledger shares
the quote database; or pass `quote=` with any complete quote payload (for
example one issued by an independent quote service) — the ledger validates the
payload structurally and **never re-runs the optimizer** for sales or refunds.

## Contract summary

### Quote

* A basket is a list of physical units with unique `unit_id`, a `sku` and a
  positive integer `price` (booleans rejected). Input order never affects the
  result.
* A coupon has a stable `coupon_id`, a positive integer `D`, and
  `requires: sku -> count`. One use consumes exactly those units, which must
  have total original price at least `D`; coupons share no physical units; each
  coupon is used at most once; untouched units stay at full price.
* The total is the minimum possible `sum(price) - sum(D)`.
* Tie-break: smaller sorted tuple of used `coupon_id`s first; if equal, compare
  the per-coupon sorted `unit_id` lists in coupon-id order.
* Each coupon's `D` is split over its units proportionally to original price:
  floor of the exact share, then one cent each to the largest fractional
  remainders, ties by `unit_id` ascending. Per-unit discount never exceeds the
  unit price and the paid amounts sum to the total.
* The service stores quotes by `request_id`: same request + canonical basket +
  snapshot replays the identical full quote; different content is
  `REQUEST_CONFLICT`. New snapshots never change old quotes.

### Ledger

* A `sale_id` binds one complete quote; the same body replays, different content
  is `SALE_CONFLICT`. Per-unit original paid amounts are persisted.
* `return(request_id, sale_id, unit_ids, grade)` refunds each unit's stored
  original paid amount, regardless of current prices/promotions. `grade` is
  `RESTOCK` or `SCRAP` per unit and drives the stock bucket.
* A unit returns at most once. A request is all-or-nothing: any already-returned
  or ineligible unit rejects the whole request, processing nothing.
* Same `request_id` + same body replays the first result (even after other
  units were returned); same id with a different body is `REQUEST_CONFLICT`.
* Refund and inventory receipt/scrap are written in one `BEGIN IMMEDIATE`
  transaction, so a failure or concurrent request never leaves a refund
  without its stock move or double-counts stock.
* `adjust_inventory(event_id, sku, quantity, bucket)` records independent
  restock/scrap events (idempotent by event id); it never changes any sale's
  refundable amounts and never resurrects returned units.
* Sales do not imply stock-out; after-sales stock starts at zero and is
  reconciled only from its own events.

### Physical coupon paper

* A **sheet** is a `rows x cols` grid with `rows, cols <= 6` and at most 12
  tickets across both faces (`tickets ≤ 12`, at most 12 sheets per event).  A
  ticket has a stable `ticket_id`, a `side` (`front`/`back`) and an inclusive
  target rectangle `[row0, col0, row1, col1]` in one shared paper coordinate
  system.  Tickets on the same face may not overlap.
* Perforations are unit edges `V r,c` (between `(r,c)` and `(r,c+1)`) and
  `H r,c` (between `(r,c)` and `(r+1,c)`).  A single tear splits one current
  rectangle along one full line, edge to edge; every unit edge the line runs
  along inside that rectangle must be punched.  `add_correction` adds holes the
  craft record missed (add-only, idempotent by `fix_id`).
* A ticket is redeemable only when some current piece **equals** its
  rectangle.  Taking that piece hands over *both faces*.  Unused pieces remain
  as physical remainder (they may be discarded later).  Two tickets printed on
  the same rectangle (front and back) can back at most one claim.
* `try_tear(sheet_id, ticket_ids)` is a pure trial returning a real ordered
  sequence of cuts, or `TEAR_INFEASIBLE`.  It never changes paper.
* `quote_group(group_id, baskets, ...)` minimises the combined paid amount of
  up to 5 baskets (each ≤ 10 units) over the paper physically present, honouring
  only `accepted_coupons` when supplied.  Every basket still uses the original
  quote rules (SKU requirements, original-price coverage, proportional
  allocation), each promotion is used at most once per basket, and each physical
  ticket goes to exactly one basket.  Ties are broken by the original
  per-basket ordering and then by basket id.  An empty honoured catalogue is
  valid (all baskets pay full price and no paper moves); only an *infeasible*
  combination raises `NO_FEASIBLE_PLAN`.
* `commit_group(request_id, sales)` re-checks the **current** pieces inside one
  `BEGIN IMMEDIATE` transaction, records the hand-over, subtracts the taken
  pieces, and registers each basket's official quote with the ledger so returns
  refund that basket's original per-unit paid amounts.  Two counters racing for
  the same sheet: one commits, the loser gets `PAPER_CONFLICT` and nothing is
  written.  Physically disjoint trials on one sheet both commit.
* Trials (`try_tear`, `quote_group(store=False)`) never move paper.  Stored
  quotes replay by `request_id`; different content is `GROUP_CONFLICT`.

### Error codes
`INVALID_BASKET`, `INVALID_COUPON`, `INVALID_INPUT`, `INVALID_GRADE`,
`INVALID_QUOTE`, `UNKNOWN_SNAPSHOT`, `SNAPSHOT_CONFLICT`, `REQUEST_CONFLICT`,
`QUOTE_CONFLICT`, `UNKNOWN_QUOTE`, `UNKNOWN_SALE`, `SALE_CONFLICT`,
`QUOTE_MISMATCH`, `UNIT_INELIGIBLE`, `UNIT_ALREADY_RETURNED`,
`OVERLAPPING_RETURN`, `INVENTORY_CONFLICT`, `INVALID_SHEET`, `INVALID_TICKET`,
`INVALID_PERFORATION`, `UNKNOWN_SHEET`, `SHEET_CONFLICT`, `TEAR_INFEASIBLE`,
`PAPER_CONFLICT`, `INVALID_GROUP`, `GROUP_CONFLICT`, `UNKNOWN_GROUP`,
`NO_FEASIBLE_PLAN`.

Every public operation either commits a complete result or raises before any
partial state is written.

## Design trade-offs

* **Exact optimization, no greedy.** A bitmask DP over ≤10 units computes the
  maximum discount; an ignored-coupon subset scan then finds the
  lexicographically smallest coupon-id set reaching it; a feasibility-guided
  prefix greedy fixes the lexicographically smallest unit-id lists. This is
  fast (milliseconds at the specified limits) and deterministic. Greedy
  "largest D first" is demonstrably wrong (see the `greedy_trap` scenario).
* **Physical paper is state, not a hint.** A sheet's current pieces are stored
  in SQLite and are the only source of truth.  `commit_group` re-runs the tear
  search on those pieces inside the write transaction, so a stale trial can
  never hand over paper that another counter already took.  Trials operate on a
  pure copy of the state and never mutate it.
* **Tear reachability is exact.** Isolating a rectangle means recursively
  splitting pieces without ever cutting across a still-pending target; the
  search is memoised on `(pieces, pending targets, punches)` and is small at
  the brief's limits (≤ 6x6, ≤ 12 tickets).
* **Joint optimisation, per-basket quotes.** The group optimiser enumerates
  feasible coupon subsets per basket, searches basket by basket under a
  suffix-discount bound, then assigns distinct physical instances.  Each chosen
  basket plan is turned back into an ordinary complete quote (the old rules),
  so the ledger binds it unchanged and refunds stay per-unit exact.  Ties use
  the original per-basket ordering; cross-basket allocation is deterministic by
  basket id.
* **Snapshot-bound quotes.** Quotes carry the snapshot id, the canonical
  basket, the coupon catalogue, assignments, the full allocation and the total.
  Passing a different ad-hoc coupon list under an existing snapshot is a
  conflict; publishing a new snapshot never mutates an old quote.
* **Ledger uses stored facts.** Refund eligibility and amounts come from the
  sale's stored unit rows (protected by primary keys and conditional updates
  inside a SQLite write transaction). Nothing about historical returns,
  condition or stock counts is inferred from a quote.
* **Concurrency.** Writers use `BEGIN IMMEDIATE` with a busy timeout; the
  unit-level guard is a conditional `UPDATE ... WHERE returned = 0` re-checked
  in-transaction, so two connections that both read "returnable" still produce
  exactly one success. This is database locking, not an in-process set.
* **Reopen.** Snapshots, quotes, sales, return requests/items, adjustments,
  inventory events and totals all live in SQLite and survive process restart.

## Layout

```
basket_service/
  __init__.py      Service facade / open_service
  errors.py        typed error codes
  validation.py    strict input + quote payload validation, allocation
  optimizer.py     exact bitmask optimizer + deterministic tie-break
  reference.py     independent brute-force / Fraction reference (tests only)
  db.py            SQLite schema and transaction helpers
  quote_service.py snapshots and quote API
  ledger.py        sales, returns, inventory, audits
  sheets.py        sheet geometry, perforations, tear reachability
  paper.py         paper state, tear sequences, joint group optimiser
  sheet_service.py sheet registry, trials, group quotes, hand-over commits
  planner.py       JSON plan runner
  demo.py          demo driver + CLI (demo / run-plan / self-check)
tests/             optimizer, quote-service, ledger, reference, paper tests
data/              real SQLite databases (created by the demo)
results/           JSON plans and result records
```

## Verification

`python3 -m basket_service demo` runs 29 checks, all computed against
independent references:

* case-material quotes vs. exhaustive enumeration;
* two sales under different promotions, refunds following each sale's own
  bound quote (including after a promotion switch);
* greedy-largest-D counterexample and contention/tie-break/remainder examples;
* a 10-unit basket optimized by full enumeration and audited from raw ledger
  rows (per-unit paid sums, refund sums, remaining refundable, stock totals);
* lost-response retry replay, whole-request overlap rejection, independent
  stock adjustments, and a two-thread concurrent return race with no double
  stock;
* the worked paper example (punched split 12, unpunched whole piece 14, cheaper
  Y 7), a real two-stage tear, a same-sheet counter race (one `PAPER_CONFLICT`),
  original-paid refunds from a paper sale, and a receipt-time perforation fix;
* reopening the database preserves everything.
