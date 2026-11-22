"""Old bit-by-bit wire: signed manifests, alphabetic prefix codes, batches.

The package mirrors the reporting-station / equipment-operator split:

* :mod:`wire.manifest`  -- a station signs off one committed directory/fact
  cut, resolves every active ticket's complete hit set into one signal, and
  tallies signal frequencies by ticket count.
* :mod:`wire.codebook`  -- the equipment side builds the minimum-bit
  length-limited alphabetic prefix code for an ordered signal table,
  honoring burned-in codewords, or declares the machine unloadable.
* :mod:`wire.batch`     -- encode a signed batch, decode it bit by bit,
  reject out-of-table signals, and archive batches under their own code
  table so old batches never change interpretation.
"""

from wire.codebook import (
    CodePlan,
    DecodeError,
    UnloadableError,
    UnknownSignal,
    build_code_plan,
    decode,
    encode,
)
from wire.manifest import (
    NONE_SIGNAL,
    ManifestError,
    Roster,
    Snapshot,
    build_roster,
    tally,
    verify_active_list,
)

__all__ = [
    "CodePlan",
    "DecodeError",
    "UnloadableError",
    "UnknownSignal",
    "build_code_plan",
    "decode",
    "encode",
    "NONE_SIGNAL",
    "ManifestError",
    "Roster",
    "Snapshot",
    "build_roster",
    "tally",
    "verify_active_list",
]
