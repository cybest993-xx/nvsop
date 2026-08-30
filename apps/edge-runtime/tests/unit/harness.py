"""Shared construction for the judgment core's unit tests.

The core is a pure function, so a test is "build a state, send events, assert on the
output" and nothing else — no clock, no sleep, no fixture process (§5.18). What repeats
across the three test files is only that building and sending, so it lives here once
rather than three times with three signatures.

Not named `test_*`, so unittest discovery does not collect it; it is imported by bare name
because the discovery start directory is on the path. That follows the precedent
`tests/contract/base/base_harness.py` sets on the contract side.
"""

from __future__ import annotations

from edge_runtime.judgment.core import advance
from edge_runtime.judgment.model import (
    Decision,
    HostInstant,
    HostLiveness,
    JudgmentState,
    Observation,
    Ordering,
    Outcome,
    RuntimeParameters,
    StreamHealth,
    Template,
    TimerFired,
)

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
