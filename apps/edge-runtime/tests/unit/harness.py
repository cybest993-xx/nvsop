"""Shared construction for the judgment core's, the supervisor's and the connectors' tests.

The core is a pure function, so a test is "build a state, send events, assert on the
output" and nothing else — no clock, no sleep, no fixture process (§5.18). What repeats
across the test files is only that building and sending, so it lives here once
rather than three times with three signatures.

The supervisor does read a clock, because it holds the timer the core declared. `FakeClock`
is that clock's stand-in: a test moves it by assignment, so a timing assertion still needs
no sleep.

Not named `test_*`, so unittest discovery does not collect it; it is imported by bare name
because the discovery start directory is on the path. That follows the precedent
`tests/contract/base/base_harness.py` sets on the contract side.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from nvsop_contracts import (
    Delivery,
    EdgePreservation,
    Measured,
    Pushed,
    Sequencing,
    TimestampSource,
)

from edge_runtime.judgment.core import advance
from edge_runtime.judgment.evidence import EvidenceClip
from edge_runtime.judgment.model import (
    Decision,
    HostInstant,
    HostLiveness,
    Instance,
    JudgmentState,
    Observation,
    Ordering,
    Outcome,
    RuntimeParameters,
    StreamHealth,
    Template,
    TimerFired,
)
from edge_runtime.local_state.queues import BackendReportContext

STEPS = ("(1) step 1", "(2) step 2", "(3) step 3", "(4) step 4", "(5) step 5")
"""A five-step template in the base's action-number format, which our `actions.json` must
keep matching (`^\\((\\d+)\\).+`). Five because §2.2's measured rework case is five steps."""

EXTERNAL_START = "工件到位"
"""An external signal as a start boundary — a physical fact, not an action number. It takes
the same code path as an action number does (§5.8)."""

EXTERNAL_END = "下料完成"

IDLE_TIMEOUT = 300.0
STEP_DEADLINE = 60.0
"""Wide enough apart that neither fires among observations a second apart, so a test about
arriving observations is not perturbed by a timer. `test_timing` drives them deliberately."""


def opening_state(
    ordering: Ordering,
    *,
    steps: tuple[str, ...] = STEPS,
    start_signal: str = STEPS[0],
    end_signals: tuple[str, ...] = (),
    idle_timeout: float = IDLE_TIMEOUT,
    step_deadline: float = STEP_DEADLINE,
) -> JudgmentState:
    """A state with no instance in flight.

    `ordering` is positional and has no default: whether the template is ordered decides
    whether a skipped number is a violation on arrival, so every test states which
    semantic it is exercising rather than inheriting one.
    """
    return JudgmentState(
        template=Template(
            steps=steps, ordering=ordering, start_signal=start_signal, end_signals=end_signals
        ),
        parameters=RuntimeParameters(idle_timeout=idle_timeout, step_deadline=step_deadline),
    )


def observe(state: JudgmentState, signal: str, at: float) -> Outcome:
    """One observation at a stated instant, returning the whole outcome."""
    return advance(state, Observation(signal=signal, at=HostInstant(at), source_time=at))


def observe_each(
    state: JudgmentState, *signals: str, start: int = 1
) -> tuple[JudgmentState, list[Decision]]:
    """One observation per signal, a second apart, collecting every decision.

    For sequence assertions, where what matters is the order signals arrive in and not the
    instants they arrive at.
    """
    decisions: list[Decision] = []
    for second, signal in enumerate(signals, start=start):
        outcome = observe(state, signal, at=float(second))
        state = outcome.state
        decisions.extend(outcome.decisions)
    return state, decisions


def fire(
    state: JudgmentState,
    at: float,
    host: HostLiveness = HostLiveness.ALIVE,
    stream: StreamHealth = StreamHealth.HEALTHY,
) -> Outcome:
    """The wake-up the core asked for, with what the supervisor found at that moment."""
    return advance(state, TimerFired(at=HostInstant(at), host=host, stream=stream))


class MemoryReactionStore:
    """在持久化接缝记录完整反应, 不复制 SQLite 或判定行为。"""

    def __init__(self) -> None:
        self.reactions: list[
            tuple[
                JudgmentState, tuple[Decision, ...], tuple[EvidenceClip, ...], tuple[Instance, ...]
            ]
        ] = []
        self.report_provenance: list[Mapping[int, tuple[BackendReportContext, ...] | None]] = []

    def commit(
        self,
        *,
        state: JudgmentState,
        decisions: Sequence[Decision],
        evidence: Sequence[EvidenceClip],
        closed_instances: Sequence[Instance],
        report_provenance: Mapping[int, tuple[BackendReportContext, ...] | None],
    ) -> None:
        self.reactions.append((state, tuple(decisions), tuple(evidence), tuple(closed_instances)))
        self.report_provenance.append(dict(report_provenance))


class FakeClock:
    """The host's monotonic clock, moved by assignment instead of by waiting.

    The supervisor reads a clock at the three points where no instant arrived with the
    input: computing how long to wait, stamping a timer firing, and stamping a run
    interruption. A test moves this one to the instant it wants to assert about, so no
    assertion sleeps and none depends on how long it took to run.
    """

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


DELIVERY_DELAY = 0.05
"""A measured delivery delay well inside any role's share of the 500 ms budget (§5.6).

A test that is about the budget states its own value; the rest inherit one that does not
make them incidentally about it.
"""


PUSHED = Pushed()
"""The default delivery mode. A module-level value rather than a call in the signature: the
declaration is frozen and shared safely, and a call there is what `B008` flags."""


def measured_capability(
    *,
    delivery: Delivery = PUSHED,
    max_delivery_delay: float = DELIVERY_DELAY,
    sequencing: Sequencing = Sequencing.SEQUENCED,
    edges: EdgePreservation = EdgePreservation.PRESERVED,
    timestamps: TimestampSource = TimestampSource.HOST_RECEIPT,
) -> Measured:
    """A declaration fit for every role, so each test states only what it is about.

    Every value is measured against a real device in production (§5.8). Here they are
    constructed, which is what lets the adapter and the fitness rule be tested before the
    device exists — the point of §5.21's 未验证 state.
    """
    return Measured(
        delivery=delivery,
        max_delivery_delay=max_delivery_delay,
        sequencing=sequencing,
        edges=edges,
        timestamps=timestamps,
    )
