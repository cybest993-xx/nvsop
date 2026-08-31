"""Watching one input point: level readings in, observations and validity facts out.

The adapter answers with a level; judgment needs an event. That conversion is this module,
and it is the whole of what stands between a connector and the judgment path — deliberately,
because §5.8 forbids judgment branching on whether a connector is configured. What leaves here
is `ExternalSignal` and `ValidityChanged`, both of which the supervisor already normalizes for
the SSE side (`supervisor/inputs.py`); a station with three connectors and one with none reach
the core through the same two types.

Nothing here judges. The two facts it reports are ones the connector holds first-hand and
nobody else can: the point could not be read, and — carried through from the adapter — the
reading could not be placed on the video timeline.

Standard library only (edge-autonomy.md §5.11).
"""

from __future__ import annotations

from edge_runtime.connectors.port import (
    InputPoint,
    PointState,
    PolledInput,
    Reading,
    ReadResult,
    Unreachable,
)
from edge_runtime.judgment.reasons import ReasonCode
from edge_runtime.supervisor.inputs import (
    ExternalSignal,
    SupervisorInput,
    Validity,
    ValidityChanged,
)


class PointWatch:
    """One input point's rising edges, and whether we can currently see it.

    Per point rather than per connector, because the state that matters is per point: the
    last level seen, and whether the last read succeeded. A connector with two inputs holds
    two of these.
    """

    def __init__(self, *, point: InputPoint) -> None:
        self._point = point
        self._state: PointState | None = None
        """The last level seen, or None when nothing has been seen since the last gap.

        Cleared on a gap rather than kept: 断线 means we stopped seeing, and holding the
        pre-gap level would let the runtime answer questions about a window it was blind for.
        """
        self._reachable = True
        """Starts true so a runtime that never fails reports nothing. The first failed read
        is the transition that reports the impairment."""

    def inputs_for(self, result: ReadResult) -> tuple[SupervisorInput, ...]:
        """What this read means for judgment, in the order to send it."""
        match result:
            case Unreachable():
                return self._lost()
            case Reading():
                return (*self._regained(), *self._edge(result))

    def _lost(self) -> tuple[SupervisorInput, ...]:
        """The point stopped answering. Reported once, and it lasts until a read succeeds.

        One per failed poll would be one per period for as long as the cable is out, and the
        core's impairment record is a set — the repeats would add nothing but volume.
        """
        self._state = None
        if not self._reachable:
            return ()
        self._reachable = False
        return (ValidityChanged(reason=ReasonCode.IO_SIGNAL_LOST, now=Validity.IMPAIRED),)

    def _regained(self) -> tuple[SupervisorInput, ...]:
        """A read succeeded after a gap.

        Sent before whatever edge this reading carries, so the pass in flight keeps the
        impairment on its record: `ValidityRestored` clears the impairment only for instances
        yet to open (`judgment/core.py`), which is exactly the semantic wanted here — the
        change may have happened while we were blind.
        """
        if self._reachable:
            return ()
        self._reachable = True
        return (ValidityChanged(reason=ReasonCode.IO_SIGNAL_LOST, now=Validity.RESTORED),)

    def _edge(self, reading: Reading) -> tuple[SupervisorInput, ...]:
        """The observation, when this reading is a change to active.

        A rising edge and not a level: an observation states that something happened at a time
        (CONTEXT.md), and a point held active across a five-second press did not happen five
        times. A falling edge is not reported at all — the template references one semantic
        label, so releasing 工件到位 would arrive at judgment as that same step a second time.

        The first reading after a gap counts as an edge when it is active. Across a gap, one
        press and two are indistinguishable; suppressing it would decide it was one, and the
        pass already carries the impairment that says this window is not concludable.
        """
        known, self._state = self._state, reading.state
        if reading.state is not PointState.ACTIVE or known is PointState.ACTIVE:
            return ()
        return (
            ExternalSignal(signal=self._point.label, at=reading.at, alignment=reading.alignment),
        )


class ConnectorPoller:
    """One polling pass over one connector's input points.

    A pass rather than a loop: **when** to poll is the run loop's business (#45), which is also
    what holds the supervisor and the SSE request. Keeping the cadence out of here is what lets
    the pass be tested without a clock or a thread — the polling period is a measured capability
    (§5.8), not a constant this module may hold.

    One unreachable point does not stop the others: a station with two connectors loses the
    judgment that depends on the one that failed, not both.
    """

    def __init__(
        self, *, connector: PolledInput, points: tuple[InputPoint, ...], timeout: float
    ) -> None:
        self._connector = connector
        self._timeout = timeout
        self._watches = tuple((point, PointWatch(point=point)) for point in points)

    def poll(self) -> tuple[SupervisorInput, ...]:
        """Read every point once, returning what it all means for judgment.

        The order within one point is the watch's; across points it is the configured order.
        Neither carries meaning for the core, which takes one event at a time.
        """
        return tuple(
            arriving
            for point, watch in self._watches
            for arriving in watch.inputs_for(self._connector.read(point, timeout=self._timeout))
        )
