"""Family two (§5.9): our reimplementation against the base, where the two must agree.

We reuse `MissingNumberDetector`'s sequence-comparison semantics and replace its boundary
source (ADR-0006). So the reused half has to keep matching, and the replaced half has to
keep differing — both are premises, and both belong here because both read `vendor/`.

Deliberately excluded from the agreement: rework and the timing of missed-step reports.
Those are exactly where we differ on purpose, and one of them is pinned below in the
opposite direction — if a subtree update fixed the base's boundary heuristic, that red test
is the signal to reread ADR-0006 rather than a regression in our code.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from base_harness import import_detector

EDGE_SRC = Path(__file__).resolve().parents[3] / "apps/edge-runtime/src"
if str(EDGE_SRC) not in sys.path:
    sys.path.insert(0, str(EDGE_SRC))

from edge_runtime.judgment import ReasonCode, Verdict  # noqa: E402
from edge_runtime.judgment.core import advance  # noqa: E402
from edge_runtime.judgment.model import (  # noqa: E402
    HostInstant,
    JudgmentState,
    Observation,
    Ordering,
    RuntimeParameters,
    Template,
)

STEPS = tuple(f"({index}) step {index}" for index in range(1, 6))
END_SIGNAL = "下料完成"


def base_results(steps: tuple[str, ...], sequence: tuple[int, ...]) -> list[dict[str, object]]:
    """Run a sequence through the base checker, one observation at a time."""
    checker = import_detector("sop_step_checker")
    cache = checker.SopCheckerCache()
    config = json.dumps({"actions": list(steps)})
    results: list[dict[str, object]] = []
    checker_id = "*"
    for number in sequence:
        response = cache.process_sop_check(
            "req",
            checker.SopCheckerRequest(
                action_json=config,
                vlm_output=steps[number - 1],
                keep_alive=True,
                checker_id=checker_id,
                cycle_completion_threshold=0.6,
                cycle_boundary_threshold_low=0.3,
                cycle_boundary_threshold_high=0.8,
            ),
        ).asdict()
        checker_id = str(response["checker_id"])
        results.append(response)
    return results


def our_missing_steps(sequence: tuple[int, ...]) -> tuple[str, ...]:
    """Run the same sequence through our core and return the steps it reports missing.

    An unordered template, because the base has no ordering declaration: comparing against
    an ordered one would compare our arrival-time reports with a semantic the base does not
    have. What is under comparison here is the set difference, which is the part we reuse.
    """
    state = JudgmentState(
        template=Template(
            steps=STEPS,
            ordering=Ordering.UNORDERED,
            start_signal=STEPS[0],
            end_signals=(END_SIGNAL,),
        ),
        parameters=RuntimeParameters(idle_timeout=300.0, step_deadline=60.0),
    )
    decisions = []
    for second, number in enumerate(sequence, start=1):
        outcome = advance(
            state,
            Observation(
                signal=STEPS[number - 1],
                at=HostInstant(float(second)),
                source_time=float(second),
            ),
        )
        state = outcome.state
        decisions.extend(outcome.decisions)

    if state.instance is not None:
        outcome = advance(
            state,
            Observation(
                signal=END_SIGNAL,
                at=HostInstant(float(len(sequence) + 1)),
                source_time=float(len(sequence) + 1),
            ),
        )
        decisions.extend(outcome.decisions)

    return tuple(
        step
        for decision in decisions
        for violation in decision.violations
        if violation.reason is ReasonCode.MISSED_STEP
        for step in violation.steps
    )


def base_missing(sequence: tuple[int, ...]) -> tuple[str, ...]:
    """The steps the base names missing across a run, as step signals.

    The base reports action numbers, so they are mapped back to the declared step they
    identify — that mapping is what our `actions.json` format exists to keep valid.
    """
    return tuple(
        STEPS[number - 1]
        for result in base_results(STEPS, sequence)
        for number in result["missing_detected"]  # type: ignore[union-attr]
    )


class WeAgreeOnCompliantSequencesTest(unittest.TestCase):
    """A pass with every step done exactly once must satisfy both implementations."""

    def test_a_complete_in_order_pass_is_compliant_on_both_sides(self) -> None:
        sequence = (1, 2, 3, 4, 5)

        base = base_results(STEPS, sequence)
        ours = our_missing_steps(sequence)

        self.assertTrue(base[-1]["cycle_completed"])
        self.assertEqual([], base[-1]["missing_detected"])
        self.assertEqual([], base[-1]["misordered_detected"])
        self.assertEqual((), ours)

    def test_a_complete_pass_closes_as_passing_on_our_side(self) -> None:
        state = JudgmentState(
            template=Template(steps=STEPS, ordering=Ordering.ORDERED, start_signal=STEPS[0]),
            parameters=RuntimeParameters(idle_timeout=300.0, step_deadline=60.0),
        )
        for second, step in enumerate(STEPS, start=1):
            outcome = advance(
                state,
                Observation(signal=step, at=HostInstant(float(second)), source_time=float(second)),
            )
            state = outcome.state

        self.assertEqual(Verdict.PASS, outcome.decisions[0].verdict)


class WeAgreeOnWhichStepsAreMissingTest(unittest.TestCase):
    """The set difference is the semantic we reuse, so the answers must match.

    Only the answer, not when it arrives: the base reports at the next pass's boundary,
    which for a 24/7 stream may be never (§2.3). Ours reports when the instance closes.
    """

    def test_the_same_steps_are_named_missing(self) -> None:
        for sequence, expected in (
            ((1, 2, 4, 5), (STEPS[2],)),
            ((1, 2, 3), (STEPS[3], STEPS[4])),
            ((1, 2, 3, 5), (STEPS[3],)),
        ):
            with self.subTest(sequence=sequence):
                # The base only produces `missing` at a boundary, so a second pass's first
                # action is appended to make it speak at all. That appended action is what
                # our side does not need, which is the point of §2.3.
                self.assertEqual(expected, base_missing((*sequence, 1)))
                self.assertEqual(expected, our_missing_steps(sequence))

    def test_a_sparse_pass_makes_the_base_silent_even_at_the_next_boundary(self) -> None:
        # Sharper than §2.3 records: the repeat that opens a boundary is itself gated on
        # having seen 60% of the steps (`cycle_completion_threshold`). A pass where the
        # operator did two of five steps never reaches that bar, so the next pass starting
        # does not make the base speak either — its two reporting moments reduce to one, and
        # for a 24/7 stream that one never comes.
        self.assertEqual((), base_missing((1, 5, 1)))

        self.assertEqual(
            (STEPS[1], STEPS[2], STEPS[3]),
            our_missing_steps((1, 5)),
            "the idle timeout and the declared end signal are time signals the base has "
            "no equivalent of, so this pass is concluded rather than dropped",
        )


class TheBaseStillMisjudgesReworkTest(unittest.TestCase):
    """The premise ADR-0006 rests on, pinned in the direction that matters.

    Our own regression asserts `1,2,3,2,4,5` is compliant. This asserts the base still is
    not, because that difference is the entire reason we reimplemented rather than patched.
    A red test here means NVIDIA changed the boundary heuristic, and ADR-0006 should be
    reread — it would not mean our code regressed.
    """

    def test_the_rework_sequence_is_still_reported_as_two_violations(self) -> None:
        base = base_results(STEPS, (1, 2, 3, 2, 4, 5))

        self.assertEqual(
            [4, 5],
            base[3]["missing_detected"],
            "the repeated step 2 must still open a cycle boundary and report steps 4 and 5 "
            "missing; if it no longer does, reread ADR-0006 and §2.2",
        )
        self.assertFalse(
            base[-1]["cycle_completed"],
            "after the false boundary the base's seen set no longer holds steps 1 and 3, so "
            "the pass cannot complete",
        )


if __name__ == "__main__":
    unittest.main()
