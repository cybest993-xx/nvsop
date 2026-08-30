"""The validity gate: "not observed" is never reported as "not done".

§2.1 is the measurement this file defends. Given one five-step template and the action
sequence `[1,2,4,5]`, the base reports `missing=[3]` whether the operator skipped step 3
or the video cut out during it — identical output, no channel to tell them apart. A shift
lead acting on the second case confronts someone over an equipment fault, and once that
happens the system is finished on that floor.

Every assertion here constructs a state, sends events and reads the output. No clock, no
sleep, no fixture process.
"""

from __future__ import annotations

import unittest

from edge_runtime.judgment import INDETERMINATE_REASONS, ReasonCode, Verdict
from edge_runtime.judgment.core import advance
from edge_runtime.judgment.model import (
    Decision,
    EvidenceSpan,
    HostInstant,
    Instance,
    JudgmentState,
    Lifecycle,
    Observation,
    Ordering,
    RunInterrupted,
    Template,
    ValidityImpaired,
    ValidityRestored,
)

STEPS = ("(1) step 1", "(2) step 2", "(3) step 3", "(4) step 4", "(5) step 5")
END_SIGNAL = "下料完成"

# Impairments the supervisor reports as they happen. The rest of the indeterminate codes
# the core derives itself, from an event of their own or from a signal outside the
# template; `test_every_indeterminate_code_has_a_producer` pins which is which.
SUPERVISOR_REPORTED = (
    ReasonCode.STREAM_LOST,
    ReasonCode.INFERENCE_BACKEND_UNREACHABLE,
    ReasonCode.INFERENCE_TIMEOUT,
    ReasonCode.TIMESTAMP_DISCONTINUITY,
    ReasonCode.CHUNK_BACKLOG_EXCEEDED,
    ReasonCode.IO_SIGNAL_LOST,
    ReasonCode.IO_TIME_UNALIGNED,
)


def opening_state(ordering: Ordering = Ordering.UNORDERED) -> JudgmentState:
    return JudgmentState(
        template=Template(
            steps=STEPS,
            ordering=ordering,
            start_signal=STEPS[0],
            end_signals=(END_SIGNAL,),
        )
    )


def observe(state: JudgmentState, *signals: str, start: int = 1) -> JudgmentState:
    for second, signal in enumerate(signals, start=start):
        state = advance(
            state,
            Observation(signal=signal, at=HostInstant(float(second)), source_time=float(second)),
        ).state
    return state


def close(state: JudgmentState, at: float) -> tuple[Decision, ...]:
    """Send the declared end signal, which is the closing condition available here."""
    return advance(
        state, Observation(signal=END_SIGNAL, at=HostInstant(at), source_time=at)
    ).decisions


class StreamLossDoesNotBecomeAMissedStepTest(unittest.TestCase):
    """§5.1: the path this gate exists to block.

    Stream drops, so chunks stop arriving, so the pass closes, so the set comparison finds
    steps missing, so the verdict is "failed" — and the operator is blamed for an
    equipment fault. The gate cuts that chain before the set comparison runs.
    """

    def test_an_instance_impaired_by_stream_loss_closes_indeterminate(self) -> None:
        state = observe(opening_state(), STEPS[0], STEPS[1])
        state = advance(state, ValidityImpaired(reason=ReasonCode.STREAM_LOST)).state

        decisions = close(state, at=9.0)

        self.assertEqual(
            Decision(
                instance_id=1,
                verdict=Verdict.INDETERMINATE,
                reasons=(ReasonCode.STREAM_LOST,),
                violations=(),
                lifecycle=Lifecycle.CLOSED_BY_END_SIGNAL,
                evidence=EvidenceSpan.at(HostInstant(9.0)),
            ),
            decisions[0],
            "steps 3, 4 and 5 were never observed, but nothing says they were not done: "
            "the instance must not enter the missing-step set comparison (§5.1)",
        )

    def test_an_impairment_that_was_restored_still_makes_the_instance_indeterminate(
        self,
    ) -> None:
        # Sight returning does not make the blind interval observable in retrospect: a step
        # absent from the set may have been performed while we could not see it.
        state = observe(opening_state(), STEPS[0])
        state = advance(state, ValidityImpaired(reason=ReasonCode.STREAM_LOST)).state
        state = advance(state, ValidityRestored(reason=ReasonCode.STREAM_LOST)).state
        state = observe(state, STEPS[1], STEPS[2], start=3)

        decisions = close(state, at=9.0)

        self.assertEqual(Verdict.INDETERMINATE, decisions[0].verdict)
        self.assertEqual((ReasonCode.STREAM_LOST,), decisions[0].reasons)

    def test_a_restored_impairment_does_not_impair_the_next_instance(self) -> None:
        state = observe(opening_state(), STEPS[0])
        state = advance(state, ValidityImpaired(reason=ReasonCode.STREAM_LOST)).state
        state = advance(state, ValidityRestored(reason=ReasonCode.STREAM_LOST)).state
        close(state, at=9.0)
        state = advance(
            state, Observation(signal=END_SIGNAL, at=HostInstant(9.0), source_time=9.0)
        ).state

        state = observe(state, *STEPS, start=10)

        self.assertEqual(frozenset(), state.active_impairments)

    def test_an_impairment_in_force_when_an_instance_opens_impairs_it(self) -> None:
        state = advance(opening_state(), ValidityImpaired(reason=ReasonCode.STREAM_LOST)).state

        state = observe(state, STEPS[0])

        assert state.instance is not None
        self.assertEqual(frozenset({ReasonCode.STREAM_LOST}), state.instance.impairments)


class EveryImpairmentReasonReachesADecisionTest(unittest.TestCase):
    """Each indeterminate code has a producer, so the front end's hint table has a source.

    An operator told "indeterminate" with no code cannot tell whether to go and look at
    vLLM or at the camera; naming the reason is most of what the third verdict is for.
    """

    def test_each_supervisor_reported_impairment_names_itself_at_the_close(self) -> None:
        for reason in SUPERVISOR_REPORTED:
            with self.subTest(reason=reason.value):
                state = observe(opening_state(), STEPS[0])
                state = advance(state, ValidityImpaired(reason=reason)).state

                decisions = close(state, at=9.0)

                self.assertEqual(Verdict.INDETERMINATE, decisions[0].verdict)
                self.assertEqual((reason,), decisions[0].reasons)

    def test_several_impairments_are_all_named(self) -> None:
        state = observe(opening_state(), STEPS[0])
        for reason in (ReasonCode.STREAM_LOST, ReasonCode.CHUNK_BACKLOG_EXCEEDED):
            state = advance(state, ValidityImpaired(reason=reason)).state

        decisions = close(state, at=9.0)

        self.assertEqual(
            (ReasonCode.STREAM_LOST, ReasonCode.CHUNK_BACKLOG_EXCEEDED),
            decisions[0].reasons,
            "an overloaded backend must show up as itself rather than degrading silently "
            "into unexplained violations (Q11)",
        )

    def test_an_impairment_event_rejects_a_violation_reason(self) -> None:
        # `ValidityImpaired(reason=MISSED_STEP)` is the safety invariant inverted: a
        # violation arriving as grounds for not concluding.
        with self.assertRaises(ValueError):
            ValidityImpaired(reason=ReasonCode.MISSED_STEP)

    def test_every_indeterminate_code_has_a_producer(self) -> None:
        core_derived = {
            ReasonCode.ACTION_ID_UNKNOWN,
            ReasonCode.RUN_INTERRUPTED,
            ReasonCode.INFERENCE_HOST_DOWN,
        }
        self.assertEqual(
            INDETERMINATE_REASONS,
            frozenset(SUPERVISOR_REPORTED) | core_derived,
            "a new indeterminate code needs a producer: either the supervisor reports it "
            "or the core derives it. Naming which keeps neither assumed",
        )


class AnUnknownActionIsIndeterminateTest(unittest.TestCase):
    """§5.2 `ACTION_ID_UNKNOWN`: template and perception disagree, so we do not guess.

    A process engineer has to be able to see that mismatch. Guessing hands them a pile of
    violations they cannot explain, when what is wrong is the template.
    """

    def test_a_signal_outside_the_template_makes_the_instance_indeterminate(self) -> None:
        state = observe(opening_state(), STEPS[0])

        outcome = advance(
            state, Observation(signal="(9) 未声明动作", at=HostInstant(3.0), source_time=3.0)
        )

        self.assertEqual(
            Decision(
                instance_id=1,
                verdict=Verdict.INDETERMINATE,
                reasons=(ReasonCode.ACTION_ID_UNKNOWN,),
                violations=(),
                lifecycle=Lifecycle.STAYS_OPEN,
                evidence=EvidenceSpan.at(HostInstant(3.0)),
            ),
            outcome.decisions[0],
        )

    def test_the_unknown_action_carries_through_to_the_close(self) -> None:
        state = observe(opening_state(), STEPS[0])
        state = advance(
            state, Observation(signal="(9) 未声明动作", at=HostInstant(3.0), source_time=3.0)
        ).state

        decisions = close(state, at=9.0)

        self.assertEqual(Verdict.INDETERMINATE, decisions[0].verdict)
        self.assertEqual((ReasonCode.ACTION_ID_UNKNOWN,), decisions[0].reasons)

    def test_an_unknown_signal_with_no_instance_in_flight_decides_nothing(self) -> None:
        outcome = advance(
            opening_state(),
            Observation(signal="(9) 未声明动作", at=HostInstant(1.0), source_time=1.0),
        )

        self.assertEqual((), outcome.decisions)
        self.assertIsNone(outcome.state.instance)


class RunInterruptionClosesInFlightWorkTest(unittest.TestCase):
    """One maintenance action must not manufacture a violation."""

    def test_an_in_flight_instance_closes_indeterminate(self) -> None:
        state = observe(opening_state(Ordering.ORDERED), STEPS[0], STEPS[1])

        outcome = advance(state, RunInterrupted(at=HostInstant(7.0)))

        self.assertEqual(
            Decision(
                instance_id=1,
                verdict=Verdict.INDETERMINATE,
                reasons=(ReasonCode.RUN_INTERRUPTED,),
                violations=(),
                lifecycle=Lifecycle.CLOSED_BY_RUN_INTERRUPTION,
                evidence=EvidenceSpan.at(HostInstant(7.0)),
            ),
            outcome.decisions[0],
            "a configuration switch or a restart is not evidence that steps 3, 4 and 5 "
            "were skipped",
        )
        self.assertIsNone(outcome.state.instance)

    def test_an_interruption_with_nothing_in_flight_decides_nothing(self) -> None:
        outcome = advance(opening_state(), RunInterrupted(at=HostInstant(7.0)))

        self.assertEqual((), outcome.decisions)


class TheSafetyInvariantHoldsTest(unittest.TestCase):
    """§5.2, stated in code rather than left to each branch:

        if evidence is insufficient or the stream is unhealthy
           or inference is unhealthy or time is unaligned:
            verdict != failed

    Every decision the core emits crosses one chokepoint, so adding a judgment path
    cannot bypass it. These assertions are that chokepoint's test.
    """

    IMPAIRMENTS = (
        ReasonCode.STREAM_LOST,
        ReasonCode.INFERENCE_BACKEND_UNREACHABLE,
        ReasonCode.IO_TIME_UNALIGNED,
        ReasonCode.CHUNK_BACKLOG_EXCEEDED,
    )

    def test_no_impaired_instance_ever_yields_a_failing_verdict(self) -> None:
        for reason in self.IMPAIRMENTS:
            for ordering in Ordering:
                with self.subTest(reason=reason.value, ordering=ordering.value):
                    state = observe(opening_state(ordering), STEPS[0])
                    state = advance(state, ValidityImpaired(reason=reason)).state

                    # A jump, which would be two violations were observation reliable.
                    outcome = advance(
                        state,
                        Observation(signal=STEPS[3], at=HostInstant(3.0), source_time=3.0),
                    )
                    closing = close(outcome.state, at=9.0)

                    for decision in (*outcome.decisions, *closing):
                        self.assertNotEqual(Verdict.FAIL, decision.verdict)
                        self.assertEqual((), decision.violations)

    def test_a_violation_latched_before_the_impairment_survives_the_indeterminate_close(
        self,
    ) -> None:
        # Story 8: "this pass's conclusion is unreliable" and "this deviation is a
        # confirmed fact" must not hide each other. The violation was confirmed while
        # observation was still good, and the close afterwards says nothing about it.
        state = observe(opening_state(Ordering.ORDERED), STEPS[0], STEPS[1])
        jump = advance(state, Observation(signal=STEPS[3], at=HostInstant(3.0), source_time=3.0))
        state = advance(jump.state, ValidityImpaired(reason=ReasonCode.STREAM_LOST)).state

        closing = close(state, at=9.0)

        self.assertEqual(Verdict.FAIL, jump.decisions[0].verdict)
        self.assertEqual(
            (ReasonCode.WRONG_STEP, ReasonCode.MISSED_STEP),
            tuple(violation.reason for violation in jump.decisions[0].violations),
        )
        self.assertEqual(Verdict.INDETERMINATE, closing[0].verdict)
        self.assertEqual((ReasonCode.STREAM_LOST,), closing[0].reasons)

    def test_a_complete_pass_still_passes_when_nothing_was_impaired(self) -> None:
        # The gate must not swallow the ordinary case: otherwise "never report a false
        # failure" would be satisfiable by never reporting anything.
        outcome = advance(
            opening_state(Ordering.ORDERED),
            Observation(signal=STEPS[0], at=HostInstant(1.0), source_time=1.0),
        )
        for second, signal in enumerate(STEPS[1:], start=2):
            outcome = advance(
                outcome.state,
                Observation(
                    signal=signal, at=HostInstant(float(second)), source_time=float(second)
                ),
            )

        self.assertEqual(Verdict.PASS, outcome.decisions[0].verdict)
        self.assertEqual((), outcome.decisions[0].reasons)


class InstanceStateIsTheSupervisorsToHoldTest(unittest.TestCase):
    """§5.18: the supervisor persists the state and passes it back in.

    So a restart resumes from a constructed state rather than from anything the core kept.
    This asserts a hand-built state behaves like one the core produced.
    """

    def test_a_reconstructed_in_flight_instance_continues(self) -> None:
        state = JudgmentState(
            template=Template(steps=STEPS, ordering=Ordering.ORDERED, start_signal=STEPS[0]),
            instance=Instance(
                instance_id=7,
                opened_at=HostInstant(100.0),
                last_observation_at=HostInstant(103.0),
                seen=frozenset(STEPS[:3]),
                expected_index=3,
                impairments=frozenset({ReasonCode.TIMESTAMP_DISCONTINUITY}),
            ),
            next_instance_id=8,
        )

        outcome = advance(
            state, Observation(signal=STEPS[4], at=HostInstant(104.0), source_time=4.0)
        )

        self.assertEqual(
            (ReasonCode.TIMESTAMP_DISCONTINUITY,),
            outcome.decisions[0].reasons,
            "the jump over step 4 would be a violation, but this instance arrived already "
            "carrying an impairment",
        )
        self.assertEqual(7, outcome.decisions[0].instance_id)


if __name__ == "__main__":
    unittest.main()
