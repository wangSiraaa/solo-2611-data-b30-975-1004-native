# Basket quote service and after-sales ledger

A standalone, Python 3.11 solution for the brief in `README.md`:

* an **optimal basket quote service** with immutable published snapshots,
  deterministic two-level tie-breaking and proportional per-unit discount
  allocation;
* an **after-sales ledger** that binds each sale to a complete published quote
  and refunds exactly the per-unit amount that sale paid, with idempotent
  requests, all-or-nothing overlap rejection, and transactional stock moves;
* a **factory-return warranty subsystem** with per-SKU terms and coupon
  addenda, half-open outage calendars, immutable recomputed versions and
  service-desk eligibility queries;
* a **repair-shop scheduler** that maximises accepted repair value against
  warranty cover, delivery days and per-specialty technician shifts.

No external dependencies are required — only the standard library and SQLite.

## Quick start

```bash
# Full demo: rebuilds data/demo.db and writes results/demo_plan.json +
# results/demo_results.json, including independent cross-checks.
python3 -m basket_service demo

# Factory-warranty + repair-shop demo (23 independently cross-checked steps).
python3 -m basket_service warranty-demo

# Unit / differential / concurrency tests.
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

# Warranty: terms, per-unit enrollment, outages, desk query.
svc.publish_warranty_term("t-base", "R", 3)
svc.publish_warranty_term("t-ext", "R", 3, coupon_id="EXT", bonus_days=2)
svc.enroll_warranties("S1", sold_day=0)
svc.register_outage("o1", "S1", "uA", 1, 3)                  # half-open [1,3)
answer = svc.query_warranty("desk-1", "S1", "uA", 7)

# Repair shop: methods, technicians, work orders, optimal batch.
svc.register_repair_method("m1", "R", duration_days=1, specialty="L1")
svc.register_technician("tech-1", "L1", unavailable=[(0, 7)])
svc.open_work_order("WO-1", "S1", "uA", delivery_day=6, repair_value=500, method_ids=["m1"])
batch = svc.plan_repairs("B1", ["WO-1"], horizon_end_day=30)
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

### Error codes

`INVALID_BASKET`, `INVALID_COUPON`, `INVALID_INPUT`, `INVALID_GRADE`,
`INVALID_QUOTE`, `UNKNOWN_SNAPSHOT`, `SNAPSHOT_CONFLICT`, `REQUEST_CONFLICT`,
`QUOTE_CONFLICT`, `UNKNOWN_QUOTE`, `UNKNOWN_SALE`, `SALE_CONFLICT`,
`QUOTE_MISMATCH`, `UNIT_INELIGIBLE`, `UNIT_ALREADY_RETURNED`,
`OVERLAPPING_RETURN`, `INVENTORY_CONFLICT`.

Every public operation either commits a complete result or raises before any
partial state is written.

### Factory-return warranty

* **Terms are published per stable SKU.** A base term carries the warranty day
  count; an optional addendum is keyed by `coupon_id` and adds bonus days. The
  addendum belongs only to a unit whose bound sale actually assigned that
  coupon at deal time — the same per-unit allocation the refund ledger freezes.
  A unit holding a coupon with no published addendum is an
  `UNKNOWN_COUPON_TERM` error rather than a silently dropped benefit.
* **The clock starts on the integer business sale day (day 0).** The calendar
  is a sequence of consecutive integer days; there are no weekends. A warranty
  budget of `N` cover-days means the expiry day is the first uncovered integer
  day, so the last legal *start* day is `expiry_day - 1`.
* **Outages are half-open `[start_day, end_day)` intervals.** Overlapping work
  orders stop the clock only once: the union of intervals is taken before any
  day is counted. Only days inside the still-live cover pause the budget; a
  brand-new outage registered after the budget ran out is recorded but reported
  as ineffectual and never revives the unit. The tail of a *back-dated* outage
  that crossed the old expiry line does extend cover, because on those business
  days the cover had not yet been exhausted.
* **Late back-dated entry/exit records are first-class.** Every event carries
  its own business day and a stable sale/record id; import order is never a
  date. Adding an outage or a return exit appends a new immutable warranty
  *version*; a service-desk query is stored against the version that answered
  it and keeps showing the customer that exact answer forever.
* **Normal returns end cover early.** `mark_return_exit` requires the unit to
  be returned in the ledger first and binds the stable return request id;
  cover ends on the earlier of the natural expiry and the effective return day
  (`end_reason = RETURNED_EXIT`).
* A desk query reports the originating coupon/tier, the budget, the cumulative
  effective paused days, any ineffectual post-expiry outage days, the expiry
  day, the latest legal start day and the expiry reason.

Warranty error codes: `UNKNOWN_TERM`, `TERM_CONFLICT`, `UNKNOWN_WARRANTY`,
`WARRANTY_CONFLICT`, `UNKNOWN_COUPON_TERM`, `UNKNOWN_RECORD`,
`RECORD_CONFLICT`, `INVALID_INTERVAL`, `RETURN_CONFLICT`.

### Repair shop

* **Catalogue.** A repair method is factory-approved for a SKU with a fixed
  consecutive duration and one required technician specialty. A technician has
  one specialty and a set of half-open unavailable intervals.
* **Work orders pin a warranty version.** Opening an order snapshots the
  unit's current warranty version; later back-dated outages never move an
  historical order. One unit has at most one open work order.
* **Feasibility.** Only the start day must be within warranty cover and not
  before the delivery day (work may finish after expiry). An accepted order
  occupies one qualified technician for the whole duration; one technician
  takes at most one unit per calendar day.
* **Exact optimisation, no greedy.** Every feasible `(method, technician,
  start-day)` placement is enumerated, then a branch-and-bound DFS (MRV order,
  value upper-bound pruning, lexicographic start/method/technician tie-break)
  maximises the total repair value of accepted orders. Deterministic
  tie-breaking makes re-runs byte-stable. A standalone brute-force placement
  scan in the demo independently confirms the optimum.
* **Decisions are explicit.** Each order is either accepted with start/end
  day, method and technician, or rejected with a reason:
  `NO_COVERED_START_DAY`, `NO_QUALIFIED_TECHNICIAN`,
  `NO_SLOT_WITHIN_HORIZON`, or `PREEMPTED_BY_HIGHER_VALUE` (feasible alone but
  displaced to maximise total value). Accepted decisions reserve the
  technician calendar; batches and decisions persist and replay idempotently.

Repair error codes: `UNKNOWN_TECHNICIAN`, `UNKNOWN_METHOD`, `UNKNOWN_BOOKING`,
`BOOKING_CONFLICT`, `UNKNOWN_WORK_ORDER`, `WORK_ORDER_CONFLICT`,
`INVALID_SPECIALTY`, `INVALID_HORIZON`.

## Design trade-offs

* **Exact optimization, no greedy.** A bitmask DP over ≤10 units computes the
  maximum discount; an ignored-coupon subset scan then finds the
  lexicographically smallest coupon-id set reaching it; a feasibility-guided
  prefix greedy fixes the lexicographically smallest unit-id lists. This is
  fast (milliseconds at the specified limits) and deterministic. Greedy
  "largest D first" is demonstrably wrong (see the `greedy_trap` scenario).
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
* **Warranty clock is a day-by-day walk, not a closed formula.** Back-dated
  outages and outages crossing the moving expiry boundary interact, so the
  engine classifies each integer day (paused vs. consumed) once and asserts an
  independent interval-union count against the walk. A post-expiry outage is
  provably ineffectual and reported separately instead of silently extending
  cover. Every recomputation is an append-only version row, so an answer given
  to a customer remains reconstructable forever.
* **Scheduling is exact branch-and-bound, never first-fit.** All feasible
  placements are materialised, conflicts are precomputed at flat-candidate
  level (same order or shared technician-day), and an MRV DFS with a value
  upper bound prunes hard while a lexicographic signature fixes tie-breaks.
  This stays instant for the stated shapes (a few complex machines over a
  hundred-day calendar, or a dozen short rush jobs over a few days) and returns
  the provably maximal total repair value; the demo's brute-force scan is the
  independent oracle.

## Layout

```
basket_service/
  __init__.py        Service facade / open_service
  errors.py          typed error codes
  validation.py      strict input + quote payload validation, allocation
  optimizer.py       exact bitmask optimizer + deterministic tie-break
  reference.py       independent brute-force / Fraction reference (tests only)
  db.py              SQLite schema and transaction helpers
  quote_service.py   snapshots and quote API
  ledger.py          sales, returns, inventory, audits
  warranty_engine.py pure half-open interval clock (pause/expiry/eligibility)
  warranty.py        terms, enrollment, outages, versions, desk queries
  repair.py          methods, technicians, work orders, exact scheduler
  warranty_demo.py   warranty + repair end-to-end driver and brute-force check
  planner.py         JSON plan runner
  demo.py            demo driver + CLI (demo / warranty-demo / run-plan / self-check)
tests/               optimizer, quote-service, ledger, reference, warranty and
                     repair-shop tests (81 tests)
data/                real SQLite databases (created by the demos)
results/             JSON plans and result records
```

## A note on the brief's outage arithmetic

The brief states that outages on days 1–3 and 2–4 "pause three days in total,
so work can still start on day 7 and the warranty expires on day 8". Under the
declared **half-open, union-once** rule, the literal intervals `[1,4)` and
`[2,5)` span four distinct calendar days (1, 2, 3, 4) and would push expiry to
day 9. The implementation follows the declared rule exactly; the warranty demo
shows both the literal four-day union (transparent disclosure) and the minimal
interval pair `[1,3) + [2,4)` whose three-day union reproduces the stated
day-7/day-8 target. No interval semantics were bent to force the headline
number.

## Verification

`python3 -m basket_service demo` runs the original 19 checks, all computed
against independent references:

* case-material quotes vs. exhaustive enumeration;
* two sales under different promotions, refunds following each sale's own
  bound quote (including after a promotion switch);
* greedy-largest-D counterexample and contention/tie-break/remainder examples;
* a 10-unit basket optimized by full enumeration and audited from raw ledger
  rows (per-unit paid sums, refund sums, remaining refundable, stock totals);
* lost-response retry replay, whole-request overlap rejection, independent
  stock adjustments, and a two-thread concurrent return race with no double
  stock;
* reopening the database preserves everything.

`python3 -m basket_service warranty-demo` runs 23 additional checks covering
the full factory-warranty and repair-shop brief, each expected value derived
independently of the production search:

* per-unit coupon allocation driving a 5-day vs. 3-day warranty budget, and a
  second equal-price sale where the coupon lands on a peer unit;
* overlapping outages counted once, the day-7/day-8 worked example, the
  literal-interval disclosure, and desk queries explaining coupon, paused days
  and expiry reason;
* a late back-dated outage creating version 2 while the version-1 desk answer
  is preserved, and a post-expiry outage reported as ineffectual (no revival);
* a normal return terminating cover on its effective business day;
* the "free only on day 7" accept/reject pair, the two-day-method vs. one-free-
  day rejection, a 14-order rush whose maximum value matches an independent
  brute-force placement scan, historical scheduling pinned to the warranty
  version at order time, and persistence across a database reopen.
