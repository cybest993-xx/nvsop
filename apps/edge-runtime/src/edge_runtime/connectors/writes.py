"""Dispatching one output point write: the capability gate, the ledger, and the record.

Three things stand between the supervisor's disposal dispatch and a relay, and all three are
here because they are the same on every adapter:

- the **capability gate**: an unverified connector is not driven at all (§5.21);
- the **ledger**, which is what makes a key idempotent — the center is at-least-once and a
  disposal dispatched before a restart is dispatched again after it (§5.7);
- the **diagnostic event**, unconditional, naming the operator and the target point, because
  driving a physical interlock is the highest-impact operation this system performs (Q37).

The adapter below this decides nothing: it reports what the device did. The retry policy is
the one judgment call in this file, and it is written out at `_replayable`.

Standard library only (edge-autonomy.md §5.11).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from edge_runtime.connectors.capability import Unverified
from edge_runtime.connectors.port import (
    Connector,
    OutputPoint,
    PointState,
    Refused,
    WriteOutcome,
    WriteRefusal,
    Written,
)

UNVERIFIED_DETAIL = "连接器能力声明未验证。不驱动物理执行器"
"""Why the write was refused, in the operator's language.

§5.21 requires the conservative reading rather than the optimistic one: 保守拒绝。不乐观放行.
"""


@dataclass(frozen=True, slots=True)
class WriteRequest:
    """One asked-for point write, with everything needed to make it safe and traceable."""

    point: OutputPoint
    state: PointState
    key: str
    """The idempotency key. Owned by the caller, because what "the same action" means is the
    caller's fact: for a disposal it is that disposal's identity, so re-dispatching after a
    restart carries the same key and drives nothing twice (§5.7)."""

    actor: str
    """Who asked. Recorded on the diagnostic event, which Q37 requires for a point write —
    a supervisor's disposal dispatch and an operator's manual test are both actors."""

    timeout: float
    """How long to wait for the device. Required rather than defaulted: a time literal on the
    physical-control path is what §5.19 forbids, so it arrives from configuration."""


@dataclass(frozen=True, slots=True)
class WriteAttempted:
    """The diagnostic event for one attempt. Q37: 记操作人与目标点位.

    A stable event name and stable fields, per Q37's decision not to build an audit table:
    diagnostics are ordinary infrastructure, and this is one event on it.
    """

    point: OutputPoint
    state: PointState
    key: str
    actor: str
    outcome: WriteOutcome
    replayed: bool
    """Whether the ledger answered instead of the device.

    Carried rather than left implicit, because "the disposal was suppressed as a duplicate"
    and "the disposal was never dispatched" are the two answers an operator asking why the
    line did not stop has to be able to tell apart.
    """


class WriteLedger(Protocol):
    """What a key has already produced, so the same action is not performed twice.

    A seam because the durable implementation is #19's: local state is SQLite (§5.7), and the
    ledger has to survive the restart it exists to protect against. `InMemoryWriteLedger` is
    the shape, and it is enough for a process that has not restarted.
    """

    def outcome_for(self, key: str, /) -> WriteOutcome | None: ...

    def record(self, key: str, outcome: WriteOutcome, /) -> None: ...


class InMemoryWriteLedger:
    """The ledger for one process's lifetime.

    Honest about what it is: idempotency across a restart needs the SQLite table #19 owns, and
    this class is what the dispatcher is tested against until that lands. Swapping it changes
    no line in `OutputDispatcher`.
    """

    def __init__(self) -> None:
        self._outcomes: dict[str, WriteOutcome] = {}

    def outcome_for(self, key: str, /) -> WriteOutcome | None:
        return self._outcomes.get(key)

    def record(self, key: str, outcome: WriteOutcome, /) -> None:
        self._outcomes[key] = outcome


class OutputDispatcher:
    """Performs the point writes the supervisor's disposal dispatch asks for.

    One per connector, because the capability declaration and the device are the connector's
    (§5.8). The ledger is passed in rather than constructed here so #19 can hand in the
    durable one.
    """

    def __init__(
        self,
        *,
        connector: Connector,
        ledger: WriteLedger,
        diagnostics: Callable[[WriteAttempted], None],
    ) -> None:
        self._connector = connector
        self._ledger = ledger
        self._diagnostics = diagnostics

    def write(self, request: WriteRequest) -> WriteOutcome:
        """Drive the point, or say why not. Never raises; the outcome is the answer.

        Structured rather than exceptional because the caller persists it: a disposal result
        is written in the same transaction as the violation it belongs to (#19), and an
        exception crossing that boundary would lose the record of what was attempted.
        """
        held = self._ledger.outcome_for(request.key)
        if held is not None and not _replayable(held):
            return self._note(request, held, replayed=True)

        if isinstance(self._connector.capability, Unverified):
            # Nothing is sent and nothing is recorded: the action did not happen, so a retry
            # once the capability has been measured must be free to proceed (§5.21).
            return self._note(
                request,
                Refused(reason=WriteRefusal.CAPABILITY_UNVERIFIED, detail=UNVERIFIED_DETAIL),
                replayed=False,
            )

        outcome = self._connector.write(request.point, request.state, timeout=request.timeout)
        self._ledger.record(request.key, outcome)
        return self._note(request, outcome, replayed=False)

    def _note(
        self, request: WriteRequest, outcome: WriteOutcome, *, replayed: bool
    ) -> WriteOutcome:
        """Emit the diagnostic event and hand the outcome back.

        Every path goes through here, including the refused and the suppressed ones, so Q37's
        "必产生事件" holds by construction rather than by each branch remembering to.
        """
        self._diagnostics(
            WriteAttempted(
                point=request.point,
                state=request.state,
                key=request.key,
                actor=request.actor,
                outcome=outcome,
                replayed=replayed,
            )
        )
        return outcome


def _replayable(held: WriteOutcome) -> bool:
    """Whether a recorded outcome leaves the action still worth attempting.

    Only an accepted write closes the key. The other three are all "the state we asked for may
    not be in force", and for a 停线联锁 that is the dangerous direction to guess in:

    - `Refused` and `Failed`: nothing physical happened, so suppressing the retry would leave
      the interlock unasserted on the strength of a failure.
    - `TimedOut`: the physical outcome is unknown. A point write assigns a level rather than
      emitting a pulse, so repeating it converges on the state that was asked for — which is
      why "may have already happened" does not argue for holding back here.

    A pulsed output, if one is ever needed, cannot use this rule and must not be added behind
    it silently: it would need the device's own idempotency, not ours.
    """
    return not isinstance(held, Written)
