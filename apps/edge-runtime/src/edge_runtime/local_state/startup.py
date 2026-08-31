"""Starting a station from persisted state: conclude what was in flight, do not resume it.

This is the third acceptance criterion in one function. A process restart is a run boundary,
and the pass that was in flight when the process died cannot be concluded on afterwards —
nobody was watching for part of it. So it closes as indeterminate with `RUN_INTERRUPTED`
(§5.7), and the next pass gets a new instance number.

Why this lives here and not in the run loop: it is three calls in a fixed order, and the
order is the semantics. A run loop that read the state and then began consuming input
without the interruption in between would resume the old pass — silently, and only visible
as a violation nobody could explain. Naming it once means the restart rule is in production
code rather than only in a test that asserts it.
"""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic

from edge_runtime.judgment.model import RuntimeParameters, Template
from edge_runtime.local_state.store import StationStore
from edge_runtime.supervisor.commands import EvidenceMargins
from edge_runtime.supervisor.station import StationSupervisor


def resume_station(
    store: StationStore,
    *,
    template: Template,
    parameters: RuntimeParameters,
    margins: EvidenceMargins,
    clock: Callable[[], float] = monotonic,
) -> StationSupervisor:
    """A supervisor ready for this run, with the previous run's pass already concluded.

    The template and the resolved parameters are the last confirmed configuration, which the
    host caches separately (§5.3) — they arrive as arguments because this store is not their
    owner.

    Returns a supervisor with nothing in flight. Any interrupted pass has been concluded and
    committed before this returns, so the caller cannot begin consuming input on a state that
    still holds it.
    """
    supervisor = StationSupervisor(
        state=store.resume(template, parameters), margins=margins, clock=clock
    )
    store.commit(supervisor, supervisor.interrupt())
    return supervisor
