"""本地状态集成测试的共同构造: 真实 supervisor 驱动真实 SQLite 迁移与反应事务。

工位 ID 与可移动时钟在此共享, 测试不在运行循环侧再次提交。
"""

from __future__ import annotations

from edge_runtime.judgment.evidence import EvidenceMargins
from edge_runtime.judgment.model import (
    Decision,
    HostInstant,
    JudgmentState,
    Ordering,
    RuntimeParameters,
    Template,
)
from edge_runtime.local_state.store import StationStore
from edge_runtime.stream_health import StreamFact, StreamHealthEvent
from edge_runtime.supervisor.inputs import ActionRecognized
from edge_runtime.supervisor.station import Reaction, StationSupervisor

STATION = "line-3/station-7"
"""One station's identity, as the center's topology names it. Local state is one database
per inference host and a host runs several stations (§5.7), so every row is scoped by it."""

OTHER_STATION = "line-3/station-8"
"""A second station in the same database, for asserting that one station's rows are not
visible to another's reads."""

STEPS = ("(1) step 1", "(2) step 2", "(3) step 3")
"""Three steps in the base's action-number format. Three rather than §2.2's five because
none of these assertions is about sequence comparison — the store persists whatever the
core concluded, and a shorter template closes in fewer events."""

IDLE_TIMEOUT = 300.0
STEP_DEADLINE = 60.0

MARGINS = EvidenceMargins(leading=5.0, trailing=5.0)

ANCHOR = 1_700_000_000.0
"""A monotonic instant far from zero, so an assertion that accidentally reads a default of
0.0 fails instead of coincidentally matching."""

SOURCE_ANCHOR = 1_699_999_000.0
"""The base's `first_timestamp` for this run. Constant across a test's actions: a change in
it is what re-anchoring is (§5.11), and no assertion here is about that."""


def opening_state(
    *,
    ordering: Ordering = Ordering.ORDERED,
    steps: tuple[str, ...] = STEPS,
    end_signals: tuple[str, ...] = (),
) -> JudgmentState:
    """A state with no instance in flight, for a station just configured."""
    return JudgmentState(
        template=Template(
            steps=steps,
            ordering=ordering,
            start_signal=steps[0],
            end_signals=end_signals,
        ),
        parameters=RuntimeParameters(idle_timeout=IDLE_TIMEOUT, step_deadline=STEP_DEADLINE),
    )


def supervisor(state: JudgmentState, clock: FakeClock, store: StationStore) -> StationSupervisor:
    """真实 supervisor 在 receive/wake/interrupt 内提交完整反应。"""
    return StationSupervisor(state=state, store=store, margins=MARGINS, clock=clock)


def action(signal: str, at: float) -> ActionRecognized:
    """One action number recognized off a chunk, at a stated monotonic instant.

    `source_anchor` is fixed for every action a test sends, because a changing anchor is
    what re-anchoring means (§5.11) and none of these assertions is about that. A test that
    wants an impaired instance says so through the health channel instead.
    """
    return ActionRecognized(
        signal=signal,
        at=HostInstant(at),
        source_time=at - ANCHOR,
        source_anchor=SOURCE_ANCHOR,
    )


def lost_stream(at: float) -> StreamHealthEvent:
    """The health channel reporting that the source failed — sight is gone from here.

    `SOURCE_ERROR` rather than a made-up "reconnecting": the base retries inside the element
    without announcing it, so this and `DELIVERING` are the only two facts a reconnect
    actually produces (§2.4).
    """
    return StreamHealthEvent(
        fact=StreamFact.SOURCE_ERROR, at_monotonic=at, source_anchor=SOURCE_ANCHOR
    )


def decision_of(reaction: Reaction) -> Decision:
    """The one decision this reaction reported, failing if it reported none or several.

    Unpacking rather than taking the first: a test that means to assert about one decision
    would otherwise pass quietly when a change makes the reaction carry two.
    """
    (decision,) = reaction.decisions
    return decision


class FakeClock:
    """The host's monotonic clock, moved by assignment instead of by waiting.

    Same role as the unit tests' clock. Duplicated rather than imported across the two test
    trees because each tree's discovery start directory is its own — importing by bare name
    across them would depend on both being on the path at once.
    """

    def __init__(self, now: float = ANCHOR) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now
