# classification-summary/1 — catalog + fact summary store

An independent catalog DAG and a fact summary library, with a single atomic
publication cut. Python 3.11, standard library only, SQLite storage.

```
cs/
  catalog.py   pure DAG algorithms: cycle check, ancestors, descendants,
               shortest witness paths, coverage explanation
  store.py     SQLite store: revisions, atomic bundles, cut-pinned queries,
               immutable products
  cli.py       command line entry point (`python -m cs`)
wire/          old bit-by-bit telegraph: signed manifests, length-limited
               alphabetic prefix codes with burned-in words, batch archives
tests/         unit tests (python -m unittest discover -s tests)
demo.py        runs materials/721 end to end and feeds consumer.py
showcase.py    walks through every required scenario on the diamond
wire_demo.py   full 15/14/17-bit telegraph workflow, bounce and archive cases
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

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite covers cycle detection on the final edge set, multi-parent and
multi-label counting, same-name distinct ids, cut-pinned re-reads, immutable
products, revision rules, withdraw/reopen, atomic bundle rejection (including
catalog rollback when a fact revision conflicts), and a concurrent reader
that must observe only pre- or post-bundle state.

## Old bit-by-bit wire (`wire/`)

A separate subsystem for the telegraph that only reads one bit at a time.
The station side and the equipment operator side have different inputs and
never share mutable state.

### Station side — `wire/manifest.py`

* A `Snapshot` is one already-published directory/fact cut (acyclic, no
  duplicate ticket). A `Roster` is the signed delivery list for one cut.
* Each active ticket is resolved against the **real directory** of that cut.
  Its complete hit set over the ordered target nodes is one signal; the
  targets are joined in manifest order. Several direct labels or several hit
  paths never multiply the ticket. A ticket that hits no target gets the
  dedicated none-signal.
* Frequencies count **tickets**, never money, so zero-amount tickets count.
  Zero-frequency signals still occupy their own slot in the signal table.
* `verify_active_list` lets any station re-derive the cut and reject a
  sign-off that omits or duplicates an active ticket.

### Equipment side — `wire/codebook.py`

`build_code_plan(signals, frequencies, max_length, burned=..., machine_id=...)`
returns the minimum-total-bit code table or raises `UnloadableError`. Every
word is non-empty binary, prefix-free, and its `0 < 1` lexicographic order
matches the signal table, so the concatenated stream decodes left to right
with no separator. Burned-in words are emitted exactly and the rest are
configurable; zero-frequency signals keep an independent word.

The builder models burned words as pinned leaves of the binary trie:

1. a depth-first trie walk produces fixed burned slots and maximal free
   *gap* nodes (a free subtree with no burned word is kept whole, not
   pre-expanded, so trees shorter than the machine width stay representable);
2. a memoized interval DP solves the length-limited alphabetic tree inside
   each gap — a singleton occupies the node, a multi-leaf node splits and
   charges the segment weight once per added edge;
3. a second interval DP assigns contiguous leaf runs across the ordered
   slots, charging each leaf for its trie depth and each burned word for its
   fixed depth. Reconstruction walks the chosen splits into concrete words.

Incompatible burned words (too long, prefixing another, or out of table
order) and widths that cannot fit the table are all declared unloadable with
the specific reason. `encode` raises `UnknownSignal` for a signal absent
from the signed table; `decode` / `decode_trace` consume bits one at a time
and reject dangling or non-binary streams.

### Batches and archives — `wire/batch.py`

A `WireBatch` seals a roster and a machine-specific plan into one immutable
record containing the full signal table, the exact code table, the ticket
order and the bit stream. Archives are append-only JSON lines; reloading an
old batch decodes it under **its own** code table, so publishing a new code
table never changes the interpretation of an already-sent batch.

### Worked numbers

For targets `A, B` and signals `A, AB, B`:

| roster frequencies | machine | minimum bits | code table |
|---|---|---|---|
| 5, 2, 3 | width 2, no burns | 15 | `0, 10, 11` |
| 2, 2, 6 (three A tickets re-routed to B) | width 2, no burns | 14 | `00, 01, 1` (short code on B) |
| 5, 2, 3 | width 2, `A` burned to `00` | 17 | `00, 01, 1` |

`python3 wire_demo.py` runs all three, prints the bit-by-bit decode back to
ticket ownership, demonstrates out-of-table bounce and unloadable machines,
and replays archived batches. The property tests cross-check the DP against
an independent exhaustive trie enumeration for every small table, and run the
stated 4–32 signal / width-12 scale in single-digit milliseconds.

