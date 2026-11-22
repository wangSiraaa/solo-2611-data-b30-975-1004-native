# classification-summary/1 — catalog + fact summary store

An independent catalog DAG and a fact summary library, with a single atomic
publication cut. Python 3.11, standard library only, SQLite storage.

```
cs/
  catalog.py   pure DAG algorithms: cycle check, ancestors, descendants,
               shortest witness paths, coverage explanation
  routing.py   pure desk-routing: reachable desks, strict-ancestor pruning,
               per-ticket check-out and dispute explanation
  handover.py  pure shift-handover solver: sealed groups, quotas, minimum
               total workhours, deterministic tie-break
  store.py     SQLite store: revisions, atomic bundles, cut-pinned queries,
               desk registry, routing/handover sign-offs, immutable products
  cli.py       command line entry point (`python -m cs`)
tests/         unit tests (python -m unittest discover -s tests)
demo.py        runs materials/721 end to end and feeds consumer.py
showcase.py    walks through every required scenario on the diamond
handover_demo.py  reproduces the two re-adjudications of the worked example
```

## Quick start

```bash
# Initialize from the supplied catalog and facts
python3 -m cs --db demo.db init materials/721/catalog.json \
    --facts materials/721/facts.jsonl

# One multi-target query pinned to one cut
python3 -m cs --db demo.db query A B

# Apply the operations JSONL (B1 accepted, B_BAD rejected)
python3 -m cs --db demo.db run materials/721/operations.jsonl --out ops.json

# Query all named requests from queries.json against a single cut and export
mkdir -p out
python3 -m cs --db demo.db query --queries materials/721/queries.json --export out
python3 materials/721/consumer.py out/overlap.json
```

`python3 demo.py` writes `demo/out/before.json` and `demo/out/after.json` and
runs the provided read-only `consumer.py` against both. `python3 showcase.py`
prints the diamond, multi-label-same-parent, parent-and-child, same-name,
withdraw/reopen, cycle rejection and recovery scenarios.

## Catalog model

* Finite directed acyclic graph. An edge is `[child, parent]`; a node may have
  several parents, so the diamond `L -> A -> R`, `L -> B -> R` is legal.
* Identity is the stable node `id`. Names are display-only and may repeat
  (`A` and `B` are both named `办件` in the sample).
* A batch of edge additions/removals is judged **only on the final edge set**.
  A cycle in the final set rejects the entire batch; operation order inside
  the batch never causes a false rejection.
* The target node is always in its own coverage (path `[target]`).
* `ancestors` / `descendants` follow child→parent / reverse edges. Coverage
  explanation reports, for each direct class, whether it can reach the target
  and one deterministic shortest path. Multiple paths or parents are not
  multiplied; only a boolean and one witness path are produced.

## Fact model

A fact revision carries `ticket_id`, `revision` (integer ≥ 1), `amount`
(non-negative integer), and a non-empty set of direct class ids.

* A ticket contributes **once** to a target if any of its direct classes can
  reach that target. Several direct classes and several paths do not multiply
  the count.
* Each target reports `count`, `amount`, its contribution list, and at least
  one hit path per contribution. Zero-amount tickets still count.
* A multi-target query may include a parent and a child. Each target reports
  its own contributions; the group `union` merges **by ticket id**, counting a
  ticket once even if it matched several targets.
* Retrying the *current* revision with identical content is accepted as a
  no-op. Retrying it with different content is rejected. A new revision number
  must be strictly greater than the current one. A withdrawn ticket needs a
  higher revision to re-open.

## Consistent publication cut

Only committed state is visible.

* **Bundle** = a batch of catalog edge changes plus a batch of fact revisions.
  Validation runs against the *projected* post-bundle catalog and fact state.
  A cycle, an unknown/illegal class, or a revision conflict aborts the whole
  bundle **before any row is written**. The validation and every write happen
  inside one `BEGIN IMMEDIATE` transaction, so a rejected bundle leaves no
  trace and a reader can only see the state before or after it, never half.
* A **cut** records `catalog_revision`, `fact_revision`, and an internal
  `fact_watermark`. Fact revisions are named `F<n>`; the catalog revision is
  `G<n>` (or the initial file revision, e.g. `G1`).
* A **query** opens one `BEGIN DEFERRED` read transaction. Its snapshot is
  fixed on the first read, so every target, the union, and any export are
  resolved from the same catalog revision and the same fact watermark. Results
  are never stitched from different points in time. A query can also be
  re-run against an explicit `Cut` to read a historical snapshot
  (`_load_facts_at_seq` replays committed facts up to that watermark).
* **Products** store their cut and a full copy of the payload. Publishing
  twice with the same product id is rejected if the content differs; later
  publications create new products and never rewrite old files. The supplied
  `consumer.py` reads a product file directly.

## Revision watermarks

`catalog_revision` is a monotonic counter over committed catalog snapshots
(`G1`, `G2`, ...). `fact_revision` is a monotonic counter over fact commits
(`F1`, `F2`, ...). `fact_watermark` is the event sequence used to replay a
historical fact state. All three are stable, real identifiers of a committed
cut; two different cuts never share the same triple.

## Edge deletion and publication cost

* Each catalog change writes a full snapshot of the projected edge set into
  `catalog_commits` (`edges` JSON) and updates the live `catalog_edges` table.
  The snapshot is what makes historical cuts reproducible: deleting an edge
  never destroys the information needed to re-evaluate an older cut.
  At the stated scale (≤ 100 nodes, ≤ 300 edges) the snapshot is tiny; the cost
  is a linear rewrite of the edge set per committed catalog revision.
* Queries do **not** maintain a transitive closure. Reachability is a reverse
  BFS from each target over the current (or snapshot) edge set, which is O(V+E)
  per target and always reflects the committed edges — there is no stale
  closure to invalidate. For a large number of targets sharing an edge set,
  the per-target BFS is repeated; at this scale that is well within budget
  (tens of targets over 1000 tickets complete in milliseconds).
* Fact updates touch only the affected ticket rows plus append-only
  `fact_commits`. Withdrawal is an `active=0` flag update, so old fact commits
  remain available for historical replay.
* A bundle that would not change any state (no edge delta and no fact advance)
  is recorded as a no-op and creates no new revision.

## Desk registry and routing (`desk-routing/1`)

A business desk (业务席) is registered on one catalog node; several desks may
share one node and display names are not identity. The registry is its own
versioned stream (`D1`, `D2`, ...) and every registry commit records the
catalog/fact cut against which its node ids were validated, so an old routing
verdict is reproducible with the registry it was signed on.

For one ticket, a desk is *reachable* when any of the ticket's direct classes
equals the desk's node or can reach it through the child→parent edges. The
reachable set is then reconciled:

* if desk 甲's node is a **strict ancestor** of desk 乙's node, the more
  specific desk 乙 is retained and 甲 is pruned (with the kept descendants
  listed as evidence);
* desks on incomparable nodes are parallel and both retained;
* desks registered on the **same node** can never be strict ancestors of one
  another, so same-node desks (including same-named ones) always survive
  together.

Each check-out keeps the full evidence per desk: covering direct classes and
one deterministic witness path, plus the retained and pruned lists. Tickets
whose retained set is empty are returned for re-registration; withdrawn
tickets are reported as not processed and never handed over. A dispute query
explains per desk why it is retained, pruned as an ancestor, or unreachable.

```bash
python3 -m cs --db demo.db register-desks materials/721/desks.json
python3 -m cs --db demo.db route T1                 # check-out, pinned to this cut
python3 -m cs --db demo.db route T1 --signoff R1    # persist the sign-off
python3 -m cs --db demo.db dispute T1 --desks R     # why was the general desk excluded?
```

## Shift handover (`handover-plan/1`)

The incoming shift brings its own roster: each desk has a fixed whole-ticket
quota. A stack consists of **sealed groups** (tickets sealed together that must
go to one desk; every member must be eligible and each occupies one slot) and
**loose tickets** (each assigned independently). Eligibility is exactly the
retained-desk set from the routing verdict at the pinned cut.

Moving a ticket to a desk different from its previous holder costs the given
per-ticket workhours (staying put costs zero). The solver enumerates feasible
assignments with branch-and-bound and returns the minimum-total-workhours
plan; equal optima resolve to one deterministic plan (sorted desk order). A
plan is only accepted when **every** unit can be placed. Otherwise the stack
is recorded as `unchanged` with concrete blockers and no roster slot moves.

* Re-signing the same `stack_id` after a catalog/registry change replaces that
  stack's previous plan: its old slots are released before re-solving, and on
  success the old sign-off is marked `superseded`.
* A *different* stack keeps its accepted slots, so a re-signing always judges
  against the currently remaining capacity ("另一单抢去名额后重新签收须按现存
  名额判"). `release-handover` winds a signed stack down while keeping its
  document on file.
* The sign-off lists each unit's actual receiving desk, per-ticket workhours
  and the net slot occupation per desk.

```bash
python3 -m cs --db demo.db roster materials/721/roster.json
python3 -m cs --db demo.db handover night-1 materials/721/stack.json H1
python3 -m cs --db demo.db show-roster night-1
python3 -m cs --db demo.db show-handover H1
```

`python3 handover_demo.py` reproduces the worked example's two
re-adjudications against real catalog bundles: original shape → zero
workhours; after deleting `L→A` the sealed pair and the loose ticket all go to
B (2 workhours); after restoring the edge with one A slot already taken by
another order, the two-ticket seal still cannot be split and goes to B whole.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite covers cycle detection on the final edge set, multi-parent and
multi-label counting, same-name distinct ids, cut-pinned re-reads, immutable
products, revision rules, withdraw/reopen, atomic bundle rejection (including
catalog rollback when a fact revision conflicts), and a concurrent reader
that must observe only pre- or post-bundle state.
