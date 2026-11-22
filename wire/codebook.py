"""Equipment side: length-limited alphabetic prefix codes with burned words.

The operator only receives a signed signal table with frequencies and a fixed
order.  Every signal gets a non-empty binary codeword such that:

* codewords are prefix-free, so the bit stream decodes left to right with no
  separator;
* lexicographic order with ``0 < 1`` matches the table order;
* no codeword exceeds the machine's maximum length;
* burned-in codewords are emitted exactly; the other words are configurable;
* even zero-frequency signals keep their own codeword.

The builder minimizes the total batch length ``sum(frequency[i] * len(code[i]))``
or raises :class:`UnloadableError` with the exact reason (a burned word that
is too long, prefixes another burned word, violates table order, or forces a
codeword past the machine's width).

Algorithm
---------
Burned words pin leaves of the binary trie.  A depth-first walk of the trie
up to ``max_length`` yields an ordered slot list: a burned slot can hold only
its named leaf, and every maximal free trie node is one gap that can hold a
contiguous run of still-unplaced leaves.  Inside one gap the problem is the
classic length-limited alphabetic (Hu-Tucker style) tree, solved exactly here
with memoized interval DP at the stated sizes (at most 32 signals, depth 12).
A second interval DP chooses how leaves are split across gaps and burned
slots, then the chosen trees are rebuilt into concrete codewords.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping, Sequence

INF = 10**18


class UnloadableError(ValueError):
    """No valid code table exists on this machine configuration."""


class UnknownSignal(KeyError):
    """A batch contains a signal that is not on the signed signal table."""


class DecodeError(ValueError):
    """A bit stream cannot be decoded under the archived code table."""


@dataclass(frozen=True)
class CodePlan:
    """One signed, machine-specific code table and its cost contract."""

    signals: tuple[str, ...]
    frequencies: tuple[int, ...]
    codewords: tuple[str, ...]
    max_length: int
    burned: Mapping[str, str]
    total_bits: int
    machine_id: str | None = None

    @property
    def code_for(self) -> dict[str, str]:
        return dict(zip(self.signals, self.codewords))


def _validate_burns(
    signals: tuple[str, ...],
    max_length: int,
    burned: Mapping[str, str],
) -> dict[int, str]:
    leaf_by_signal = {signal: idx for idx, signal in enumerate(signals)}
    leaf_burn: dict[int, str] = {}
    for signal, code in burned.items():
        if signal not in leaf_by_signal:
            raise UnloadableError(f"burned signal {signal!r} is not on the signal table")
        if not code or any(bit not in "01" for bit in code):
            raise UnloadableError(f"burned code for {signal!r} must be a non-empty binary string")
        if len(code) > max_length:
            raise UnloadableError(
                f"burned code {code!r} for {signal!r} exceeds machine width {max_length}"
            )
        leaf_burn[leaf_by_signal[signal]] = code

    ordered = sorted(leaf_burn.items(), key=lambda item: item[1])
    for (leaf_a, code_a), (leaf_b, code_b) in zip(ordered, ordered[1:]):
        if code_a == code_b:
            raise UnloadableError(f"two signals share burned code {code_a!r}")
        if code_b.startswith(code_a):
            raise UnloadableError(
                f"burned code {code_a!r} is a prefix of burned code {code_b!r}: no prefix-free table"
            )
        if leaf_a > leaf_b:
            raise UnloadableError(
                f"burned codes {code_a!r}/{code_b!r} are not in signal-table order"
            )
    return leaf_burn


def _trie_slots(
    leaf_burn: dict[int, str],
    max_length: int,
) -> list[tuple[str, int | None]]:
    """Depth-first trie walk yielding (prefix, burned leaf index or ``None``).

    Three kinds of nodes exist:

    * a node whose word is burned is a fixed slot for that leaf and is never
      expanded, so nothing else can live under a burned word;
    * a free node whose subtree contains no burned word is one *gap* slot that
      may hold a contiguous run of leaves shaped by the interval DP -- free
      tries are not pre-expanded, otherwise length-limited trees shorter than
      the machine width could never be represented;
    * a free node whose subtree does contain a burned word must be expanded so
      that its fixed slots reach their pinned positions.

    A free node at the maximum depth is necessarily a one-leaf gap.
    """

    burned_at_prefix = {code: leaf for leaf, code in leaf_burn.items()}

    def subtree_burns(prefix: str) -> set[int]:
        return {
            leaf
            for code, leaf in burned_at_prefix.items()
            if code == prefix or code.startswith(prefix + "0") or code.startswith(prefix + "1")
        }

    slots: list[tuple[str, int | None]] = []
    stack: list[str] = [""]
    while stack:
        prefix = stack.pop()
        if prefix in burned_at_prefix:
            slots.append((prefix, burned_at_prefix[prefix]))
            continue
        if len(prefix) == max_length or not subtree_burns(prefix):
            # Whole free subtree becomes one configurable gap slot.
            slots.append((prefix, None))
            continue
        # Stack is LIFO: push 1 first so 0 is processed first, preserving
        # lexicographic order in the emitted slot sequence.
        stack.append(prefix + "1")
        stack.append(prefix + "0")
    return slots


def build_code_plan(
    signals: Sequence[str],
    frequencies: Sequence[int],
    max_length: int,
    *,
    burned: Mapping[str, str] | None = None,
    machine_id: str | None = None,
) -> CodePlan:
    """Build the minimum-total-bit loadable plan, or raise ``UnloadableError``."""

    signal_tuple = tuple(signals)
    freq_tuple = tuple(frequencies)
    if not signal_tuple:
        raise UnloadableError("empty signal table")
    if len(signal_tuple) != len(freq_tuple):
        raise UnloadableError("signals and frequencies have different lengths")
    if any(freq < 0 for freq in freq_tuple):
        raise UnloadableError("frequencies must be non-negative ticket counts")
    if max_length < 1:
        raise UnloadableError("machine width must be at least one bit")
    burned = dict(burned or {})
    n = len(signal_tuple)
    leaf_burn = _validate_burns(signal_tuple, max_length, burned)
    slots = _trie_slots(leaf_burn, max_length)

    prefix_weights = [0]
    for freq in freq_tuple:
        prefix_weights.append(prefix_weights[-1] + freq)

    def segment_weight(left: int, right: int) -> int:
        """Weight of leaves ``[left, right)``."""

        return prefix_weights[right] - prefix_weights[left]

    # ---- gap DP: best alphabetic tree for leaves [left, right) inside a free
    # trie node, with ``height`` bits of depth budget still available below
    # that node.  Zero-cost leaves make every state finite once feasible.
    @lru_cache(maxsize=None)
    def gap_cost(left: int, right: int, height: int) -> int:
        """Minimum internal cost of leaves ``[left, right)`` in one free gap.

        Standard length-limited alphabetic-tree interval DP:

        * a single leaf placed at the current node costs nothing here -- the
          edge reaching this node is charged by the node's parent (or by the
          allocation DP for the gap node itself);
        * splitting ``[left, right)`` into two ordered child subtrees adds one
          edge under every leaf of the segment, i.e. the full segment weight,
          plus the recursively optimal costs of both child subtrees.
        """

        count = right - left
        if count == 0:
            return 0
        if count == 1:
            # A leaf needs no internal edge below the node it occupies.  The
            # node itself must exist, which every caller guarantees.
            return 0 if height >= 0 else INF
        if height < 1:
            # More than one leaf still needs at least one split edge.
            return INF
        best = INF
        for cut in range(left + 1, right):
            child = gap_cost(left, cut, height - 1) + gap_cost(cut, right, height - 1)
            if child >= INF:
                continue
            cost = child + segment_weight(left, right)
            if cost < best:
                best = cost
        return best

    @lru_cache(maxsize=None)
    def gap_split(left: int, right: int, height: int) -> int:
        best = INF
        best_cut = left + 1
        for cut in range(left + 1, right):
            child = gap_cost(left, cut, height - 1) + gap_cost(cut, right, height - 1)
            if child >= INF:
                continue
            cost = child + segment_weight(left, right)
            if cost < best:
                best = cost
                best_cut = cut
        return best_cut

    def build_gap(left: int, right: int, height: int, prefix: str) -> dict[int, str]:
        """Rebuild the subtree chosen by :func:`gap_cost`/``gap_split``.

        Invariant with the cost DP: a singleton leaf occupies the node named
        by ``prefix`` directly (its incoming edge was charged by the parent),
        so no extra bit is appended here.  The allocation DP handles the
        special case of the whole table being one leaf at the trie root, where
        a non-empty codeword still has to be emitted.
        """

        count = right - left
        if count == 0:
            return {}
        if count == 1:
            return {left: prefix}
        cut = gap_split(left, right, height)
        result = build_gap(left, cut, height - 1, prefix + "0")
        result.update(build_gap(cut, right, height - 1, prefix + "1"))
        return result

    # ---- allocation DP: walk the ordered trie slots, assign leaf ranges.
    @lru_cache(maxsize=None)
    def alloc_cost(slot_index: int, leaf: int) -> int:
        if slot_index == len(slots):
            return 0 if leaf == n else INF
        _prefix, burned_leaf = slots[slot_index]
        if burned_leaf is not None:
            if burned_leaf != leaf:
                return INF
            # The burned word's own bits are fixed and still cost per ticket.
            burned_depth = len(_prefix)
            return (
                segment_weight(leaf, leaf + 1) * burned_depth
                + alloc_cost(slot_index + 1, leaf + 1)
            )
        depth = len(_prefix)
        best = INF
        # Option A: leave this free gap unused and carry the same leaves to
        # the next trie slot.  This is what absorbs gaps created only to make
        # room for a burned word in a different branch.
        skip = alloc_cost(slot_index + 1, leaf)
        if skip < best:
            best = skip
        # Option B: place a non-empty contiguous run [leaf, end) in this gap.
        for end in range(leaf + 1, n + 1):
            inside = gap_cost(leaf, end, max_length - depth)
            if inside >= INF:
                continue
            # gap_cost counts edges strictly inside the subtree; every leaf
            # also pays the trie depth of the gap node itself.  A singleton
            # landing on the trie root would otherwise have an empty word, so
            # it still consumes one edge (its only codeword bit).
            root_edge = 1 if depth == 0 and end - leaf == 1 else 0
            cost = inside + segment_weight(leaf, end) * (depth + root_edge)
            total = cost + alloc_cost(slot_index + 1, end)
            if total < best:
                best = total
        return best

    @lru_cache(maxsize=None)
    def alloc_choice(slot_index: int, leaf: int) -> tuple[bool, int]:
        """Return (use_this_gap, end_leaf); skipped gaps report ``(False, leaf)``."""

        _prefix, burned_leaf = slots[slot_index]
        if burned_leaf is not None:
            return (True, leaf + 1)
        depth = len(_prefix)
        best = alloc_cost(slot_index + 1, leaf)
        best_choice = (False, leaf)
        for end in range(leaf + 1, n + 1):
            inside = gap_cost(leaf, end, max_length - depth)
            if inside >= INF:
                continue
            root_edge = 1 if depth == 0 and end - leaf == 1 else 0
            total = (
                inside
                + segment_weight(leaf, end) * (depth + root_edge)
                + alloc_cost(slot_index + 1, end)
            )
            if total < best:
                best = total
                best_choice = (True, end)
        return best_choice

    total = alloc_cost(0, 0)
    if total >= INF:
        raise UnloadableError(
            f"machine width {max_length} cannot fit {n} signals under the burned-in codes"
        )

    # ---- rebuild concrete codewords by replaying the chosen allocation.
    codewords: dict[int, str] = {}
    leaf = 0
    for slot_index, (prefix, burned_leaf) in enumerate(slots):
        use, end = alloc_choice(slot_index, leaf)
        if burned_leaf is not None:
            codewords[burned_leaf] = prefix
            leaf += 1
            continue
        if not use:
            continue
        depth = len(prefix)
        if depth == 0 and end - leaf == 1:
            # Whole table is one signal in the root gap: emit a non-empty word.
            codewords[leaf] = "0"
        else:
            codewords.update(build_gap(leaf, end, max_length - depth, prefix))
        leaf = end
    if leaf != n or len(codewords) != n:
        raise UnloadableError("internal reconstruction failed to place every signal")

    ordered_words = tuple(codewords[idx] for idx in range(n))
    _check_table(signal_tuple, freq_tuple, ordered_words, max_length, burned)
    actual_total = sum(freq * len(word) for freq, word in zip(freq_tuple, ordered_words))
    if actual_total != total:
        # Guard against any accounting drift between the two DPs and reality.
        raise UnloadableError("internal cost accounting mismatch")
    return CodePlan(
        signals=signal_tuple,
        frequencies=freq_tuple,
        codewords=ordered_words,
        max_length=max_length,
        burned=dict(burned),
        total_bits=actual_total,
        machine_id=machine_id,
    )


def _check_table(
    signals: tuple[str, ...],
    frequencies: tuple[int, ...],
    codewords: tuple[str, ...],
    max_length: int,
    burned: Mapping[str, str],
) -> None:
    if any(not word or any(bit not in "01" for bit in word) for word in codewords):
        raise UnloadableError("every codeword must be a non-empty binary string")
    if any(len(word) > max_length for word in codewords):
        raise UnloadableError("a reconstructed codeword exceeds the machine width")
    for earlier, later in zip(codewords, codewords[1:]):
        if later.startswith(earlier):
            raise UnloadableError(f"reconstructed words {earlier!r}/{later!r} are not prefix-free")
        if earlier >= later:
            raise UnloadableError("reconstructed codewords are not in 0-before-1 table order")
    for signal, code in burned.items():
        idx = signals.index(signal)
        if codewords[idx] != code:
            raise UnloadableError(f"reconstruction moved burned code for {signal!r}")


# ---------------------------------------------------------------------------
# Encoding and bit-by-bit decoding
# ---------------------------------------------------------------------------


def encode(plan: CodePlan, signal_sequence: Sequence[str]) -> str:
    """Concatenate codewords with no separator; out-of-table signals bounce."""

    code_for = plan.code_for
    pieces: list[str] = []
    for signal in signal_sequence:
        if signal not in code_for:
            raise UnknownSignal(f"signal {signal!r} is not on the signed table: batch must be rejected")
        pieces.append(code_for[signal])
    return "".join(pieces)


def decode(plan: CodePlan, bitstream: str) -> list[str]:
    """Decode a stream left to right, one bit at a time, with no lookahead."""

    trie: dict[str, object] = {}
    for signal, word in zip(plan.signals, plan.codewords):
        node = trie
        for bit in word:
            node = node.setdefault(bit, {})  # type: ignore[assignment]
        if "" in node:
            raise DecodeError("duplicate codeword in code table")
        node[""] = signal

    decoded: list[str] = []
    node = trie
    for position, bit in enumerate(bitstream, start=1):
        if bit not in "01":
            raise DecodeError(f"bit {position} is not binary: {bit!r}")
        if bit not in node:  # type: ignore[operator]
            raise DecodeError(f"bit {position}: no codeword prefix matches the stream")
        node = node[bit]  # type: ignore[assignment]
        if "" in node:
            decoded.append(node[""])
            node = trie
    if node is not trie:
        raise DecodeError("stream ends in the middle of a codeword")
    return decoded


def decode_trace(plan: CodePlan, bitstream: str) -> list[tuple[str, str]]:
    """Like :func:`decode` but returns ``(consumed_bits, signal)`` steps."""

    trie: dict[str, object] = {}
    for signal, word in zip(plan.signals, plan.codewords):
        node = trie
        for bit in word:
            node = node.setdefault(bit, {})  # type: ignore[assignment]
        node[""] = signal

    steps: list[tuple[str, str]] = []
    node = trie
    start = 0
    for position, bit in enumerate(bitstream, start=1):
        if bit not in node:  # type: ignore[operator]
            raise DecodeError(f"bit {position}: no codeword prefix matches the stream")
        node = node[bit]  # type: ignore[assignment]
        if "" in node:
            steps.append((bitstream[start:position], node[""]))
            start = position
            node = trie
    if node is not trie:
        raise DecodeError("stream ends in the middle of a codeword")
    return steps
