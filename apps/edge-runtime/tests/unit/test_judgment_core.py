"""The judgment core's own regressions: construct a state, send events, assert output.

No clock, no sleep, no fixture process, no mock. That is what the core's purity buys
(judgment-and-boundary.md §5.18): if one of these needed a patched clock to write, the
interface would be wrong, not the test.

These assertions do not touch `vendor/`. The ones that do — our sequence comparison
agreeing with the base on a compliant sequence — stay in `tests/contract/base/`, so a
red `make contract-base` means a base premise moved rather than our logic regressing
(§5.9).
"""

from __future__ import annotations

import unittest

from edge_runtime.judgment import ReasonCode, Verdict
from edge_runtime.judgment.core import advance
from edge_runtime.judgment.model import (
    Decision,
    EvidenceSpan,
    HostInstant,
    JudgmentState,
    Lifecycle,
    Observation,
    Ordering,
    RuntimeParameters,
    Template,
    Violation,
)

STEPS = ("(1) step 1", "(2) step 2", "(3) step 3", "(4) step 4", "(5) step 5")

# Long enough that neither can fire within the second-apart observations below. Time-driven
# conclusions have their own file; these assertions are about arriving observations.
PARAMETERS = RuntimeParameters(idle_timeout=300.0, step_deadline=60.0)


def opening_state(
    *,
    steps: tuple[str, ...] = STEPS,
    ordering: Ordering = Ordering.ORDERED,
    start_signal: str = STEPS[0],
    end_signals: tuple[str, ...] = (),
) -> JudgmentState:
    return JudgmentState(
        template=Template(
            steps=steps, ordering=ordering, start_signal=start_signal, end_signals=end_signals
        ),
        parameters=PARAMETERS,
    )


def run(state: JudgmentState, *steps: str) -> tuple[JudgmentState, list[Decision]]:
    """Send one observation per step, one second apart, and collect every decision."""
    decisions: list[Decision] = []
    for second, signal in enumerate(steps, start=1):
        outcome = advance(
            state,
            Observation(signal=signal, at=HostInstant(float(second)), source_time=float(second)),
        )
        state = outcome.state
        decisions.extend(outcome.decisions)
    return state, decisions


class ReworkIsCompliantTest(unittest.TestCase):
    """§2.2: the sharpest line between our behavior and the base's.

    Five-step SOP, the operator does 1,2,3, reworks 2, then continues 4,5 — one wholly
    compliant pass. The base's boundary heuristic reads the repeated number as a new
    cycle starting, clears `seen_in_cycle`, and reports that pass as two separate
    violations: `missing=[4,5]` on the repeat and `final_missing=[1,3]` at the end.

    Rework is normal on a factory floor. A false alarm here is what makes the floor stop
    trusting the alarms, and a system nobody trusts gets worked around.
    """

    def test_the_rework_sequence_closes_as_one_passing_instance(self) -> None:
        state = opening_state()

        _, decisions = run(state, STEPS[0], STEPS[1], STEPS[2], STEPS[1], STEPS[3], STEPS[4])

        self.assertEqual(
            [
                Decision(
                    instance_id=1,
                    verdict=Verdict.PASS,
                    reasons=(),
                    violations=(),
                    lifecycle=Lifecycle.CLOSED_BY_COMPLETE_SET,
                    evidence=EvidenceSpan.at(HostInstant(6.0)),
                )
            ],
            decisions,
            "the repeated step 2 must not open a boundary, clear the seen set, or produce "
            "a missing-step violation (§2.2, ADR-0006)",
        )

    def test_repeating_the_start_signal_does_not_open_a_second_instance(self) -> None:
        # The instance boundary is the first satisfaction of the declared start signal.
        # Reworking step 1 is rework, not a new pass.
        state = opening_state()

        state, decisions = run(state, STEPS[0], STEPS[1], STEPS[0], STEPS[2])

        self.assertEqual([], decisions)
        assert state.instance is not None
        self.assertEqual(1, state.instance.instance_id)
        self.assertEqual(2, state.next_instance_id)


class InstanceBoundaryTest(unittest.TestCase):
    """§5.1: the boundary is declared, never inferred from the sequence's shape."""

    def test_an_observation_before_the_start_signal_opens_nothing(self) -> None:
        state = opening_state(start_signal="工件到位")

        outcome = advance(
            state,
            Observation(signal=STEPS[1], at=HostInstant(1.0), source_time=1.0),
        )

        self.assertIsNone(
            outcome.state.instance,
            "only the declared start signal opens an instance; opening on any recognized "
            "action would turn rework and fetching parts into false instances (ADR-0009)",
        )
        self.assertEqual((), outcome.decisions)

    def test_an_external_start_signal_opens_the_instance_without_being_a_step(self) -> None:
        # The station has a connector, so the template declares a physical fact as the
        # start signal. The core takes the same path as a station with no connector: the
        # signal is an observation like any other (§5.8).
        state = opening_state(start_signal="工件到位")

        state, decisions = run(state, "工件到位", *STEPS)

        self.assertEqual(Lifecycle.CLOSED_BY_COMPLETE_SET, decisions[-1].lifecycle)
        self.assertEqual(Verdict.PASS, decisions[-1].verdict)

    def test_a_declared_end_signal_closes_the_instance(self) -> None:
        state = opening_state(end_signals=("下料完成",))

        state, decisions = run(state, STEPS[0], STEPS[1], STEPS[2], STEPS[3], "下料完成")

        self.assertEqual(
            Decision(
                instance_id=1,
                verdict=Verdict.FAIL,
                reasons=(ReasonCode.MISSED_STEP,),
                violations=(
                    Violation(
                        reason=ReasonCode.MISSED_STEP,
                        steps=(STEPS[4],),
                        evidence=EvidenceSpan.at(HostInstant(5.0)),
                    ),
                ),
                lifecycle=Lifecycle.CLOSED_BY_END_SIGNAL,
                evidence=EvidenceSpan.at(HostInstant(5.0)),
            ),
            decisions[-1],
        )

    def test_a_complete_step_set_closes_the_instance_immediately(self) -> None:
        # The base's `cycle_completed` fast path, kept: `len(seen) == N` (§5.1).
        state = opening_state(ordering=Ordering.UNORDERED)

        state, decisions = run(state, STEPS[0], STEPS[2], STEPS[1], STEPS[4], STEPS[3])

        self.assertEqual(1, len(decisions))
        self.assertEqual(Lifecycle.CLOSED_BY_COMPLETE_SET, decisions[0].lifecycle)
        self.assertIsNone(state.instance)

    def test_the_next_instance_opens_after_the_previous_one_closed(self) -> None:
        state = opening_state()

        state, _ = run(state, *STEPS)
        outcome = advance(state, Observation(signal=STEPS[0], at=HostInstant(9.0), source_time=9.0))

        assert outcome.state.instance is not None
        self.assertEqual(2, outcome.state.instance.instance_id)
        self.assertEqual(frozenset({STEPS[0]}), outcome.state.instance.seen)


class MissedStepIsReportedOnClosingTest(unittest.TestCase):
    """§2.3: the base stays silent on the shift's last pass. This must not."""

    def test_an_unordered_template_reports_missing_steps_when_the_instance_closes(
        self,
    ) -> None:
        state = opening_state(ordering=Ordering.UNORDERED, end_signals=("下料完成",))

        state, decisions = run(state, STEPS[0], STEPS[2], STEPS[4], "下料完成")

        self.assertEqual(
            (
                Violation(
                    reason=ReasonCode.MISSED_STEP,
                    steps=(STEPS[1],),
                    evidence=EvidenceSpan.at(HostInstant(4.0)),
                ),
                Violation(
                    reason=ReasonCode.MISSED_STEP,
                    steps=(STEPS[3],),
                    evidence=EvidenceSpan.at(HostInstant(4.0)),
                ),
            ),
            decisions[-1].violations,
            "each missing step is its own latched fact, anchored at the closing moment "
            "(evidence-and-retention.md §5.20)",
        )

    def test_an_unordered_template_reports_no_violation_for_order(self) -> None:
        # An unordered template's steps may be done in any order; reporting order there
        # would be the false alarm story 11 is about.
        state = opening_state(ordering=Ordering.UNORDERED)

        _, decisions = run(state, STEPS[0], STEPS[4], STEPS[3], STEPS[2], STEPS[1])

        self.assertEqual([Verdict.PASS], [decision.verdict for decision in decisions])


class OrderedTemplateReportsOnArrivalTest(unittest.TestCase):
    """§5.1: an ordered template reports a skipped number when it is skipped.

    Without this, real-time error-proofing does not deliver for missed steps — the most
    common of the four violation kinds.
    """

    def test_skipping_a_step_reports_the_wrong_step_and_the_missed_step_at_once(
        self,
    ) -> None:
        state = opening_state()

        state, decisions = run(state, STEPS[0], STEPS[1], STEPS[3])

        anchor = EvidenceSpan.at(HostInstant(3.0))
        self.assertEqual(
            [
                Decision(
                    instance_id=1,
                    verdict=Verdict.FAIL,
                    reasons=(ReasonCode.MISSED_STEP, ReasonCode.WRONG_STEP),
                    violations=(
                        Violation(reason=ReasonCode.WRONG_STEP, steps=(STEPS[3],), evidence=anchor),
                        Violation(
                            reason=ReasonCode.MISSED_STEP, steps=(STEPS[2],), evidence=anchor
                        ),
                    ),
                    lifecycle=Lifecycle.STAYS_OPEN,
                    evidence=anchor,
                )
            ],
            decisions,
            "the arriving step is the wrong one to be doing now, and the skipped step is "
            "missing: two facts, one anchor, reported without waiting for the close",
        )

    def test_a_step_arriving_after_a_later_one_is_out_of_order(self) -> None:
        state = opening_state()

        state, decisions = run(state, STEPS[0], STEPS[1], STEPS[3], STEPS[2])

        self.assertEqual(
            (
                Violation(
                    reason=ReasonCode.OUT_OF_ORDER,
                    steps=(STEPS[2],),
                    evidence=EvidenceSpan.at(HostInstant(4.0)),
                ),
            ),
            decisions[-1].violations,
        )
        self.assertEqual((ReasonCode.OUT_OF_ORDER,), decisions[-1].reasons)

    def test_a_step_already_reported_missing_is_not_reported_missing_again(self) -> None:
        state = opening_state(end_signals=("下料完成",))

        state, decisions = run(state, STEPS[0], STEPS[3], "下料完成")

        missed = [
            violation
            for decision in decisions
            for violation in decision.violations
            if violation.reason is ReasonCode.MISSED_STEP
        ]
        self.assertEqual(
            [(STEPS[1],), (STEPS[2],), (STEPS[4],)],
            [violation.steps for violation in missed],
            "steps 2 and 3 are reported when the jump is detected and must not be "
            "reported a second time at the close; step 5 is only known missing then",
        )

    def test_a_latched_violation_makes_the_closing_verdict_fail(self) -> None:
        # A violation is a confirmed fact; the operator making the step up afterwards
        # does not remove it (§5.2). The instance still closes, and it closes failing.
        state = opening_state()

        state, decisions = run(state, STEPS[0], STEPS[1], STEPS[3], STEPS[2], STEPS[4])

        self.assertEqual(Lifecycle.CLOSED_BY_COMPLETE_SET, decisions[-1].lifecycle)
        self.assertEqual(Verdict.FAIL, decisions[-1].verdict)
        self.assertEqual(
            (),
            decisions[-1].violations,
            "the close repeats no already-latched violation; it carries the verdict",
        )


class TemplateValidationTest(unittest.TestCase):
    def test_a_template_declares_at_least_one_step(self) -> None:
        with self.assertRaises(ValueError):
            opening_state(steps=())

    def test_template_steps_are_distinct(self) -> None:
        with self.assertRaises(ValueError):
            opening_state(steps=(STEPS[0], STEPS[0]))


if __name__ == "__main__":
    unittest.main()
