# Basket quote service, after-sales ledger and cash change office

A standalone, Python 3.11 solution for the brief in `README.md`:

* an **optimal basket quote service** with immutable published snapshots,
  deterministic two-level tie-breaking and proportional per-unit discount
  allocation;
* an **after-sales ledger** that binds each sale to a complete published quote
  and refunds exactly the per-unit amount that sale paid, with idempotent
  requests, all-or-nothing overlap rejection, and transactional stock moves;
* a **cash receipt and change office** for the teaching store: integer-cent
  contract accounting plus a separate multiple-of-five cash ledger, IOU
  generation, sealed-bag minimum-freight transport planning and exact-coin
  disbursement, all reopenable and independently auditable.

No external dependencies are required — only the standard library and SQLite.

## Quick start

```bash
# Full demo: rebuilds data/demo.db and writes results/demo_plan.json +
# results/demo_results.json, including independent cross-checks.
python3 -m basket_service demo

# Unit / differential / concurrency tests (71 tests).
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

# Cash sale: counters, round5 receipts, IOUs and change-cart planning.
svc.register_counter("C1")
svc.register_sale("S2", quote=cash_quote, payment_method="CASH", counter_id="C1")
svc.return_units("r2", "S2", ["a"], {"a": "RESTOCK"}, slot=0)  # cash receipt + maybe IOU
svc.register_bag("BAG1", coins={5: 1}, delivers_to=["C1"], arrival_slot=0, freight=2)
plan = svc.plan_day()                  # exact minimum-freight, whole-bag plan
svc.save_plan("P1", plan)
svc.apply_plan(plan_id="P1")           # deliveries + disbursements, idempotent
svc.close_day()                        # every receipt re-opened and checked to the cent
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

## Cash receipt and change-office contract

### Two ledgers, two units

The exact ledger never changes: `sale_units` / `return_items` keep integer
cents and every per-unit refund is still the stored original paid amount.
The cash ledger only ever stores multiples of five and never feeds back into
the exact totals. `round5(n) = 5 * ((n + 2) // 5)`: remainders 0, 1 and 2 round
down, 3 and 4 round up.

### Cash receipts (`return_units(..., slot=k)` on a `CASH` sale)

* A sale is registered with `payment_method="CASH"` plus an existing
  `counter_id`; `TRANSFER` sales (the default) keep the original contract and
  reject a `slot`.
* When the exact cumulative refund of one cash sale reaches `R`, the cash
  cumulative owed is `round5(R)`. A receipt's **actual cash** is the
  difference between the new and previous rounded cumulatives — never a
  rounded per-request difference — so rounding credit/debit cancels over the
  sale and the final cash total equals `round5(sale total)`.
* A zero-difference receipt still flips the units to returned (the original
  return eligibility is consumed) and is fully queryable, but creates no IOU.
* The exact refund, its inventory move, the receipt and any IOU are inserted
  in one `BEGIN IMMEDIATE` transaction. A lost-receipt retry replays the
  stored receipt/IOU; two tellers racing on the same unit still produce
  exactly one return, one receipt and one IOU (the loser gets
  `UNIT_ALREADY_RETURNED`). A re-entered unrelated unit or unknown sale is
  rejected before any cash row exists, so it can never mint an IOU.
* One counter holds at most one **positive** IOU per slot
  (`IOU_SLOT_TAKEN`); zero-due receipts share a slot freely.
* After-sales queries return the per-unit exact amounts, the actual cash
  difference and both cumulatives (`get_return`, `get_receipt`,
  `list_receipts`).

### Change cart and coin office

* Denominations are only **5, 10 and 25**; customers take exact sums, so no
  overpayment or change is allowed.
* A sealed bag is registered once with its exact coin counts, the counters it
  may deliver to, its arrival slot and a fixed freight. It is delivered whole
  to one eligible counter at most once; it can never be split or sent twice.
* `plan_day` solves the **exact** minimum-freight assignment over all pending
  IOUs with an exhaustive branch-and-bound search (budget-guarded with
  `PLAN_TOO_LARGE` rather than returning an unverified answer): bags are only
  considered when their arrival slot is not after the IOU slot, coin payments
  use deterministic largest-denomination-first backtracking that preserves
  small change for later IOUs, and equal-freight plans break ties
  lexicographically. Infeasible days return `feasible: false` with the
  blocking IOU named.
* `apply_plan` re-simulates the whole plan against the live database and
  rejects drifted plans (`PLAN_CONFLICT` / `INSUFFICIENT_COINS` /
  `IOU_ALREADY_PAID`) before moving any coin; committed plans are idempotent
  on replay and never issue coins twice. `pay_iou` allows a manual exact
  tender with the same guarantees; retrying the original disbursement id
  replays it, while a new id against a settled IOU is refused.
* Unpaid IOUs keep their original ids and status; failed tenders spend
  nothing.

### Cash error codes

`UNKNOWN_COUNTER`, `UNKNOWN_BAG`, `UNKNOWN_RECEIPT`, `UNKNOWN_IOU`,
`UNKNOWN_PLAN`, `UNKNOWN_DISBURSEMENT`, `IOU_SLOT_TAKEN`, `IOU_ALREADY_PAID`,
`IOU_CONFLICT`, `INSUFFICIENT_COINS`, `INCORRECT_TENDER`, `BAG_CONFLICT`,
`PLAN_CONFLICT`, `PLAN_TOO_LARGE`, `INVALID_DENOMINATION`.

### Independent re-open and day close

`audit_cash` recomputes every coin balance from raw `cash_office_events`,
re-derives every receipt's rounded cumulative and cash difference from its
exact cumulative, verifies one positive IOU per counter/slot, that only
positive receipts carry IOUs, and that disbursed + pending IOU totals match.
`close_day` additionally re-sums each receipt's per-unit exact rows, so the
day clerk can re-open the cash books to the cent from any single cash
receipt. All state is SQLite-backed and survives process restart.

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

## Layout

```
basket_service/
  __init__.py      Service facade / open_service
  errors.py        typed error codes
  validation.py    strict input + quote payload validation, allocation
  optimizer.py     exact bitmask optimizer + deterministic tie-break
  reference.py     independent brute-force / Fraction reference (tests only)
  db.py            SQLite schema, cash-office tables and migration helpers
  quote_service.py snapshots and quote API
  ledger.py        sales, returns, inventory, audits, cash receipt issuance
  cash.py          round5, receipts/IOUs, bags, coin office, day planner
  planner.py       JSON plan runner
  demo.py          demo driver + CLI (demo / run-plan / self-check)
tests/             optimizer, quote-service, ledger, cash-office, reference tests
data/              real SQLite databases (created by the demo)
results/           JSON plans and result records
```

## Verification

`python3 -m basket_service demo` runs 36 checks, all computed against
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
* cash: the two real two-unit quotes (first IOU 5 vs. 0, whole-order cash 10
  in both), round5 boundaries, minimum-freight whole-bag routing, the
  10-then-5 change-squeeze case, idempotent plan replay without double coins,
  slot uniqueness and day-close recomputation to the cent;
* reopening the database preserves everything.
