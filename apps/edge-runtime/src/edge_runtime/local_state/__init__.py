"""The inference host's local state: the authority for what it has judged and what it owes.

`open_local_state(path)` is the entry point; `LocalState.station(id)` scopes it to one
station. `resume_station` is the restart path — it is what makes an interrupted pass conclude
rather than resume (§5.7).

The center's `monitor` holds a mirror of what is here, written by idempotent upsert. This is
the original: it keeps working while the center is unreachable, and nothing on the
error-proofing path waits for the center to agree.
"""

from __future__ import annotations

from edge_runtime.local_state.queues import PendingEvidence, PendingReport, StationQueues
from edge_runtime.local_state.startup import resume_station
from edge_runtime.local_state.store import LocalState, StationStore, open_local_state

__all__ = [
    "LocalState",
    "PendingEvidence",
    "PendingReport",
    "StationQueues",
    "StationStore",
    "open_local_state",
    "resume_station",
]
