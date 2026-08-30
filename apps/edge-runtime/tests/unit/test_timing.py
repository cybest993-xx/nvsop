"""Time-driven conclusions: the idle timeout closes a pass, the step deadline reports one.

Both are measured on the host's monotonic clock, which is the only clock that can measure
chunks *ceasing to arrive* — a chunk timestamp cannot, because there is no chunk (§5.1).

The core owns both verdicts but holds no timer: it declares when it needs waking and the
supervisor sets it, which keeps all four violation kinds in one place while leaving the
core a pure function (§5.18). So these assertions send a `TimerFired` event; none of them
sleeps or patches a clock.
"""

from __future__ import annotations

import unittest

from edge_runtime.judgment import ReasonCode, Verdict
from edge_runtime.judgment.core import advance
from edge_runtime.judgment.model import (
    Decision,
    EvidenceSpan,
    HostInstant,
    HostLiveness,
    JudgmentState,
    Lifecycle,
    Observation,
    Ordering,
    Outcome,
    RuntimeParameters,
    StreamHealth,
    Template,
    TimerFired,
    ValidityImpaired,
    Violation,
)

STEPS = ("(1) step 1", "(2) step 2", "(3) step 3", "(4) step 4", "(5) step 5")
IDLE_TIMEOUT = 300.0
STEP_DEADLINE = 60.0


def opening_state(ordering: Ordering = Ordering.UNORDERED) -> JudgmentState:
    return JudgmentState(
        template=Template(steps=STEPS, ordering=ordering, start_signal=STEPS[0]),
        parameters=RuntimeParameters(idle_timeout=IDLE_TIMEOUT, step_deadline=STEP_DEADLINE),
    )


def observe(state: JudgmentState, signal: str, at: float) -> Outcome:
    return advance(state, Observation(signal=signal, at=HostInstant(at), source_time=at))


def fire(
    state: JudgmentState,
    at: float,
    host: HostLiveness = HostLiveness.ALIVE,
    stream: StreamHealth = StreamHealth.HEALTHY,
) -> Outcome:
    return advance(state, TimerFired(at=HostInstant(at), host=host, stream=stream))


class TheCoreDeclaresWhenItNeedsWakingTest(unittest.TestCase):
    def test_the_wake_up_is_the_nearer_of_the_step_deadline_and_the_idle_timeout(
        self,
    ) -> None:
        outcome = observe(opening_state(), STEPS[0], at=100.0)

        self.assertEqual(HostInstant(160.0), outcome.wake_at)

    def test_the_idle_timeout_is_the_wake_up_once_the_deadline_has_been_reported(
        self,
    ) -> None:
        state = observe(opening_state(), STEPS[0], at=100.0).state

        outcome = fire(state, at=160.0)

        self.assertEqual(
            HostInstant(400.0),
            outcome.wake_at,
            "the deadline for this wait is already reported, so the next thing that can "
            "happen by time alone is the idle close",
        )

    def test_both_timeouts_are_measured_on_the_monotonic_clock(self) -> None:
        # The two clocks are deliberately far apart. A chunk timestamp is relative to the
        # stream's start, so a long-running stream's monotonic instant and its source time
        # diverge — and only the monotonic one can measure chunks ceasing to arrive. Were
        # the core to use `source_time`, this wake-up would land at 62.0.
        outcome = observe(opening_state(), STEPS[0], at=900.0)

        self.assertEqual(HostInstant(960.0), outcome.wake_at)

    def test_an_observation_restarts_both_measurements(self) -> None:
        state = observe(opening_state(), STEPS[0], at=100.0).state

        outcome = observe(state, STEPS[1], at=140.0)

        self.assertEqual(HostInstant(200.0), outcome.wake_at)

    def test_no_wake_up_is_declared_once_the_instance_closed(self) -> None:
        state = opening_state()
        for second, signal in enumerate(STEPS, start=1):
            outcome = observe(state, signal, at=float(second))
            state = outcome.state

        self.assertIsNone(outcome.state.instance)
        self.assertIsNone(outcome.wake_at)

    def test_a_timer_firing_with_nothing_in_flight_decides_nothing(self) -> None:
        outcome = fire(opening_state(), at=160.0)

        self.assertEqual((), outcome.decisions)
        self.assertIsNone(outcome.wake_at)


class TheIdleTimeoutClosesThePassTest(unittest.TestCase):
    """§2.3: the base never reports the shift's last pass. This is the missing signal.

    Its only two reporting moments are the next pass's boundary and the stream ending, and
    for a 24/7 stream the second never comes.
    """

    def test_a_healthy_idle_close_reports_the_missing_steps(self) -> None:
        state = opening_state()
        for second, signal in enumerate((STEPS[0], STEPS[1], STEPS[2]), start=1):
            state = observe(state, signal, at=float(second)).state

        outcome = fire(state, at=303.0)

        self.assertEqual(
            Decision(
                instance_id=1,
                verdict=Verdict.FAIL,
                reasons=(ReasonCode.MISSED_STEP,),
                violations=(
                    Violation(
                        reason=ReasonCode.MISSED_STEP,
                        steps=(STEPS[3],),
                        evidence=EvidenceSpan.at(HostInstant(303.0)),
                    ),
                    Violation(
                        reason=ReasonCode.MISSED_STEP,
                        steps=(STEPS[4],),
                        evidence=EvidenceSpan.at(HostInstant(303.0)),
                    ),
                ),
                lifecycle=Lifecycle.CLOSED_BY_IDLE_TIMEOUT,
                evidence=EvidenceSpan.at(HostInstant(303.0)),
            ),
            outcome.decisions[0],
            "the closing anchor is the moment the condition held, which is the "
            "instance-closing latency class's start point (§5.6)",
        )
        self.assertIsNone(outcome.state.instance)

    def test_the_idle_close_wins_when_both_conditions_hold_at_once(self) -> None:
        # A configuration where the deadline is not shorter than the idle timeout. The pass
        # is over, so reporting a deadline for a step nobody is waiting on any more would
        # be noise on top of the close.
        state = JudgmentState(
            template=Template(steps=STEPS, ordering=Ordering.UNORDERED, start_signal=STEPS[0]),
            parameters=RuntimeParameters(idle_timeout=60.0, step_deadline=60.0),
        )
        state = observe(state, STEPS[0], at=10.0).state

        outcome = fire(state, at=70.0)

        self.assertEqual(Lifecycle.CLOSED_BY_IDLE_TIMEOUT, outcome.decisions[0].lifecycle)
        self.assertEqual((ReasonCode.MISSED_STEP,), outcome.decisions[0].reasons)


class TheStepDeadlineIsAViolationNotAClosingConditionTest(unittest.TestCase):
    """§5.2 `DEADLINE_EXCEEDED`: the pass is late, and it is still going.

    The verdict stays in the core with the other three kinds because they share this
    instance's state; splitting it would put violation logic in two places (§5.18).
    """

    def test_waiting_past_the_deadline_reports_a_violation_and_keeps_the_pass_open(
        self,
    ) -> None:
        state = observe(opening_state(Ordering.ORDERED), STEPS[0], at=100.0).state

        outcome = fire(state, at=160.0)

        self.assertEqual(
            Decision(
                instance_id=1,
                verdict=Verdict.FAIL,
                reasons=(ReasonCode.DEADLINE_EXCEEDED,),
                violations=(
                    Violation(
                        reason=ReasonCode.DEADLINE_EXCEEDED,
                        steps=(STEPS[1],),
                        evidence=EvidenceSpan(
                            anchor=HostInstant(160.0),
                            required_from=HostInstant(100.0),
                            required_to=HostInstant(160.0),
                        ),
                    ),
                ),
                lifecycle=Lifecycle.STAYS_OPEN,
                evidence=EvidenceSpan.at(HostInstant(160.0)),
            ),
            outcome.decisions[0],
        )
        self.assertIsNotNone(outcome.state.instance)

    def test_the_required_span_covers_the_whole_wait(self) -> None:
        # Story 18: a reviewer has to see how long it actually waited, so the required span
        # runs from the last observation to the moment the deadline was crossed. The
        # configured margins may widen that; they cannot shorten it (§5.20).
        state = observe(opening_state(Ordering.ORDERED), STEPS[0], at=100.0).state

        violation = fire(state, at=160.0).decisions[0].violations[0]

        self.assertEqual(HostInstant(160.0), violation.evidence.anchor)
        self.assertEqual(HostInstant(100.0), violation.evidence.required_from)
        self.assertEqual(
            60.0,
            violation.evidence.required_to.seconds - violation.evidence.required_from.seconds,
            "the required span is the wait itself, which is longer than the 5 s default "
            "margin either side of the anchor",
        )

    def test_an_ordered_template_names_the_step_being_waited_on(self) -> None:
        state = opening_state(Ordering.ORDERED)
        state = observe(state, STEPS[0], at=10.0).state
        state = observe(state, STEPS[1], at=20.0).state

        violation = fire(state, at=80.0).decisions[0].violations[0]

        self.assertEqual((STEPS[2],), violation.steps)

    def test_an_unordered_template_names_every_step_still_outstanding(self) -> None:
        state = observe(opening_state(Ordering.UNORDERED), STEPS[0], at=10.0).state

        violation = fire(state, at=70.0).decisions[0].violations[0]

        self.assertEqual(STEPS[1:], violation.steps)

    def test_the_same_wait_is_not_reported_twice(self) -> None:
        state = observe(opening_state(Ordering.ORDERED), STEPS[0], at=100.0).state
        state = fire(state, at=160.0).state

        outcome = fire(state, at=230.0)

        self.assertEqual(
            (),
            outcome.decisions,
            "the supervisor may wake the core again for its own reasons; one late wait is one fact",
        )

    def test_the_deadline_arms_again_for_the_next_step(self) -> None:
        state = observe(opening_state(Ordering.ORDERED), STEPS[0], at=100.0).state
        state = fire(state, at=160.0).state
        state = observe(state, STEPS[1], at=200.0).state

        outcome = fire(state, at=260.0)

        self.assertEqual(
            (STEPS[2],),
            outcome.decisions[0].violations[0].steps,
            "a second step running late is a second fact about how this station is paced",
        )

    def test_a_late_pass_still_closes_failing_on_the_idle_timeout(self) -> None:
        state = observe(opening_state(Ordering.ORDERED), STEPS[0], at=100.0).state
        state = fire(state, at=160.0).state

        outcome = fire(state, at=400.0)

        self.assertEqual(Lifecycle.CLOSED_BY_IDLE_TIMEOUT, outcome.decisions[0].lifecycle)
        self.assertEqual(Verdict.FAIL, outcome.decisions[0].verdict)
        self.assertEqual(
            (ReasonCode.MISSED_STEP, ReasonCode.DEADLINE_EXCEEDED),
            outcome.decisions[0].reasons,
            "the close names both what this pass missed and that it ran late",
        )


class TheGateAlsoCoversTimeDrivenVerdictsTest(unittest.TestCase):
    """The path §5.1 names explicitly: stream drops, chunks stop, the timer fires.

    Without the gate, the set comparison then finds every remaining step missing and the
    operator is blamed for an equipment fault.
    """

    def test_the_timer_finding_the_stream_lost_closes_indeterminate(self) -> None:
        # The supervisor may never have sent an impairment event — the loss is what stopped
        # the chunks, and the supervisor finds it when it looks at this firing.
        state = observe(opening_state(), STEPS[0], at=10.0).state

        outcome = fire(state, at=310.0, stream=StreamHealth.LOST)

        self.assertEqual(
            Decision(
                instance_id=1,
                verdict=Verdict.INDETERMINATE,
                reasons=(ReasonCode.STREAM_LOST,),
                violations=(),
                lifecycle=Lifecycle.CLOSED_BY_IDLE_TIMEOUT,
                evidence=EvidenceSpan.at(HostInstant(310.0)),
            ),
            outcome.decisions[0],
        )

    def test_the_timer_finding_the_process_gone_closes_indeterminate(self) -> None:
        # Process death cannot announce itself: the stream-health channel only delivers
        # while the process and its pipeline live (§5.11). Continued silence is the signal.
        state = observe(opening_state(), STEPS[0], at=10.0).state

        outcome = fire(state, at=310.0, host=HostLiveness.DOWN)

        self.assertEqual((ReasonCode.INFERENCE_HOST_DOWN,), outcome.decisions[0].reasons)

    def test_a_deadline_crossed_while_the_stream_was_lost_is_not_a_violation(self) -> None:
        # We cannot tell a slow operator from a step performed unseen.
        state = observe(opening_state(Ordering.ORDERED), STEPS[0], at=100.0).state
        state = advance(state, ValidityImpaired(reason=ReasonCode.STREAM_LOST)).state

        outcome = fire(state, at=160.0, stream=StreamHealth.LOST)

        self.assertEqual(Verdict.INDETERMINATE, outcome.decisions[0].verdict)
        self.assertEqual((), outcome.decisions[0].violations)

    def test_a_suppressed_deadline_does_not_wake_the_core_again_for_the_same_wait(
        self,
    ) -> None:
        state = observe(opening_state(Ordering.ORDERED), STEPS[0], at=100.0).state
        state = advance(state, ValidityImpaired(reason=ReasonCode.STREAM_LOST)).state

        outcome = fire(state, at=160.0, stream=StreamHealth.LOST)

        self.assertEqual(HostInstant(400.0), outcome.wake_at)


class RuntimeParameterValidationTest(unittest.TestCase):
    """Rejected where the value is built, not discovered on the floor as odd behavior."""

    def test_a_non_positive_idle_timeout_is_rejected(self) -> None:
        # Zero would close every instance at its first observation.
        for value in (0.0, -1.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RuntimeParameters(idle_timeout=value, step_deadline=STEP_DEADLINE)

    def test_a_non_positive_step_deadline_is_rejected(self) -> None:
        for value in (0.0, -1.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RuntimeParameters(idle_timeout=IDLE_TIMEOUT, step_deadline=value)


if __name__ == "__main__":
    unittest.main()
