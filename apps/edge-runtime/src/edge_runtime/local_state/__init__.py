"""The inference host's local state: the authority for what it has judged and what it owes.

`open_local_state(path)` is the entry point. `LocalState.station(id)` scopes judgment state to
one station; `LocalState.reports()` owns host-level decision/instance reconciliation. Restart
orchestration belongs to `supervisor.startup`, which passes only domain data through this
package's persistence seam (§5.7).

The center's `monitor` holds a mirror of what is here, written by idempotent upsert. This is
the original: it keeps working while the center is unreachable, and nothing on the
error-proofing path waits for the center to agree.
"""

from __future__ import annotations

from edge_runtime.local_state.queues import (
    BackendReportContext,
    PendingEvidence,
    PendingReport,
    PendingSopInstanceReport,
    ReportContext,
)
from edge_runtime.local_state.store import (
    LocalState,
    ReactionStore,
    ReportStore,
    StationStore,
    open_local_state,
)

__all__ = [
    "BackendReportContext",
    "LocalState",
    "PendingEvidence",
    "PendingReport",
    "PendingSopInstanceReport",
    "ReactionStore",
    "ReportContext",
    "ReportStore",
    "StationStore",
    "open_local_state",
]
