"""Turning what the core described into what the supervisor is told to do.

The core describes and executes nothing (§5.18). This seam is the translation: one decision
becomes an explicit command per effect, so the ticket that adds persistence, disposal and
upload has a named thing to implement rather than a decision object to re-interpret.

The one piece of judgment that happens here is the evidence window: the core declares the
anchor and the required span, and the supervisor widens it by the station's configured
margins (§5.20). Widening only — a configured window may never shorten what the conclusion
requires.
"""

from __future__ import annotations

import unittest

from harness import STEPS

from edge_runtime.judgment import ReasonCode, Verdict
from edge_runtime.judgment.model import (
    Decision,
    EvidenceSpan,
    HostInstant,
    Lifecycle,
    Violation,
)
from edge_runtime.supervisor.commands import (
    ClipEvidence,
    CloseInstance,
    EvidenceMargins,
    LatchViolation,
    RecordDecision,
    commands_for,
)

MARGINS = EvidenceMargins(leading=5.0, trailing=5.0)
"""The §5.19 defaults, passed in as data. The supervisor holds no time literal of its own:
these are the station's resolved runtime parameters (§5.21)."""


def deadline_violation(at: float, since: float) -> Violation:
    """A deadline violation, whose required span is the whole wait rather than an instant."""
    return Violation(
        reason=ReasonCode.DEADLINE_EXCEEDED,
        steps=(STEPS[1],),
        evidence=EvidenceSpan.spanning(HostInstant(at), since=HostInstant(since)),
    )


class EachEffectBecomesOneCommandTest(unittest.TestCase):
    def test_a_passing_close_records_the_decision_clips_and_closes(self) -> None:
        decision = Decision(
            instance_id=7,
            verdict=Verdict.PASS,
            reasons=(),
            violations=(),
            lifecycle=Lifecycle.CLOSED_BY_COMPLETE_SET,
            evidence=EvidenceSpan.at(HostInstant(100.0)),
        )

        self.assertEqual(
            (
                RecordDecision(decision=decision),
                ClipEvidence(
                    instance_id=7,
                    anchor=HostInstant(100.0),
                    start=HostInstant(95.0),
                    end=HostInstant(105.0),
                ),
                CloseInstance(instance_id=7, lifecycle=Lifecycle.CLOSED_BY_COMPLETE_SET),
            ),
            commands_for(decision, margins=MARGINS),
            "a pass still gets a clip: compliance rate needs a denominator, and whether "
            "this deployment keeps pass-class evidence is a retention policy, not "
            "something to decide by never producing the clip (§5.19)",
        )

    def test_each_violation_becomes_its_own_latch_command(self) -> None:
        anchor = EvidenceSpan.at(HostInstant(50.0))
        violations = (
            Violation(reason=ReasonCode.WRONG_STEP, steps=(STEPS[3],), evidence=anchor),
            Violation(reason=ReasonCode.MISSED_STEP, steps=(STEPS[1],), evidence=anchor),
        )
        decision = Decision(
            instance_id=3,
            verdict=Verdict.FAIL,
            reasons=(ReasonCode.MISSED_STEP, ReasonCode.WRONG_STEP),
            violations=violations,
            lifecycle=Lifecycle.STAYS_OPEN,
            evidence=anchor,
        )

        commands = commands_for(decision, margins=MARGINS)

        self.assertEqual(
            (
                LatchViolation(instance_id=3, violation=violations[0]),
                LatchViolation(instance_id=3, violation=violations[1]),
            ),
            tuple(command for command in commands if isinstance(command, LatchViolation)),
        )

    def test_a_decision_that_stays_open_carries_no_close_command(self) -> None:
        decision = Decision(
            instance_id=3,
            verdict=Verdict.FAIL,
            reasons=(ReasonCode.WRONG_STEP,),
            violations=(
                Violation(
                    reason=ReasonCode.WRONG_STEP,
                    steps=(STEPS[3],),
                    evidence=EvidenceSpan.at(HostInstant(50.0)),
                ),
            ),
            lifecycle=Lifecycle.STAYS_OPEN,
            evidence=EvidenceSpan.at(HostInstant(50.0)),
        )

        commands = commands_for(decision, margins=MARGINS)

        self.assertEqual((), tuple(c for c in commands if isinstance(c, CloseInstance)))

    def test_the_decision_is_recorded_before_anything_is_latched_or_clipped(self) -> None:
        decision = Decision(
            instance_id=3,
            verdict=Verdict.FAIL,
            reasons=(ReasonCode.DEADLINE_EXCEEDED,),
            violations=(deadline_violation(at=160.0, since=100.0),),
            lifecycle=Lifecycle.STAYS_OPEN,
            evidence=EvidenceSpan.at(HostInstant(160.0)),
        )

        commands = commands_for(decision, margins=MARGINS)

        self.assertIsInstance(
            commands[0],
            RecordDecision,
            "the violation and its report event are written in one transaction with the "
            "decision they belong to (#19), so the decision leads",
        )


class MarginsWidenTheRequiredSpanAndNeverShortenItTest(unittest.TestCase):
    """§5.20: configuration can widen evidence, never cut it."""

    def test_a_required_span_longer_than_the_window_survives_whole(self) -> None:
        decision = Decision(
            instance_id=1,
            verdict=Verdict.FAIL,
            reasons=(ReasonCode.DEADLINE_EXCEEDED,),
            violations=(deadline_violation(at=160.0, since=100.0),),
            lifecycle=Lifecycle.STAYS_OPEN,
            evidence=EvidenceSpan.at(HostInstant(160.0)),
        )

        clips = [c for c in commands_for(decision, margins=MARGINS) if isinstance(c, ClipEvidence)]

        self.assertEqual(
            [
                ClipEvidence(
                    instance_id=1,
                    anchor=HostInstant(160.0),
                    # 95.0 rather than 155.0: the wait began at 100.0 and the reviewer has
                    # to see the whole of it (story 18). The 5 s margin widens that start.
                    start=HostInstant(95.0),
                    end=HostInstant(165.0),
                )
            ],
            clips,
        )

    def test_zero_margins_leave_the_required_span_exactly(self) -> None:
        decision = Decision(
            instance_id=1,
            verdict=Verdict.FAIL,
            reasons=(ReasonCode.DEADLINE_EXCEEDED,),
            violations=(deadline_violation(at=160.0, since=100.0),),
            lifecycle=Lifecycle.STAYS_OPEN,
            evidence=EvidenceSpan.at(HostInstant(160.0)),
        )

        clips = [
            c
            for c in commands_for(decision, margins=EvidenceMargins(leading=0.0, trailing=0.0))
            if isinstance(c, ClipEvidence)
        ]

        self.assertEqual(
            [
                ClipEvidence(
                    instance_id=1,
                    anchor=HostInstant(160.0),
                    start=HostInstant(100.0),
                    end=HostInstant(160.0),
                )
            ],
            clips,
        )

    def test_a_negative_margin_is_rejected_where_it_is_configured(self) -> None:
        with self.assertRaises(ValueError):
            EvidenceMargins(leading=-1.0, trailing=5.0)


class OneClipPerAnchorTest(unittest.TestCase):
    """Violations sharing an anchor share a clip; a different anchor gets its own.

    The decision and the violations it carries usually anchor at the same instant, and
    cutting the same seconds of video twice is waste the reviewer never sees.
    """

    def test_violations_at_the_decision_anchor_produce_one_clip(self) -> None:
        anchor = EvidenceSpan.at(HostInstant(300.0))
        decision = Decision(
            instance_id=2,
            verdict=Verdict.FAIL,
            reasons=(ReasonCode.MISSED_STEP,),
            violations=(
                Violation(reason=ReasonCode.MISSED_STEP, steps=(STEPS[3],), evidence=anchor),
                Violation(reason=ReasonCode.MISSED_STEP, steps=(STEPS[4],), evidence=anchor),
            ),
            lifecycle=Lifecycle.CLOSED_BY_IDLE_TIMEOUT,
            evidence=anchor,
        )

        clips = [c for c in commands_for(decision, margins=MARGINS) if isinstance(c, ClipEvidence)]

        self.assertEqual(
            [
                ClipEvidence(
                    instance_id=2,
                    anchor=HostInstant(300.0),
                    start=HostInstant(295.0),
                    end=HostInstant(305.0),
                )
            ],
            clips,
        )

    def test_a_violation_anchored_elsewhere_gets_its_own_clip(self) -> None:
        decision = Decision(
            instance_id=2,
            verdict=Verdict.FAIL,
            reasons=(ReasonCode.DEADLINE_EXCEEDED, ReasonCode.MISSED_STEP),
            violations=(
                Violation(
                    reason=ReasonCode.MISSED_STEP,
                    steps=(STEPS[3],),
                    evidence=EvidenceSpan.at(HostInstant(300.0)),
                ),
                deadline_violation(at=200.0, since=140.0),
            ),
            lifecycle=Lifecycle.CLOSED_BY_IDLE_TIMEOUT,
            evidence=EvidenceSpan.at(HostInstant(300.0)),
        )

        clips = [c for c in commands_for(decision, margins=MARGINS) if isinstance(c, ClipEvidence)]

        self.assertEqual(
            [
                ClipEvidence(
                    instance_id=2,
                    anchor=HostInstant(300.0),
                    start=HostInstant(295.0),
                    end=HostInstant(305.0),
                ),
                ClipEvidence(
                    instance_id=2,
                    anchor=HostInstant(200.0),
                    start=HostInstant(135.0),
                    end=HostInstant(205.0),
                ),
            ],
            clips,
        )


if __name__ == "__main__":
    unittest.main()
