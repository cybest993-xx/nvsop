"""原 Command 用例迁至领域持久化接缝: 判定、锁存、闭合和证据窗口不变。"""

from __future__ import annotations

import unittest

from store_harness import MARGINS, STATION, opening_state

from edge_runtime.judgment.evidence import EvidenceClip, EvidenceMargins
from edge_runtime.judgment.model import (
    Decision,
    EvidenceSpan,
    HostInstant,
    Instance,
    Lifecycle,
    Violation,
)
from edge_runtime.judgment.reasons import ReasonCode, Verdict
from edge_runtime.local_state import open_local_state
from edge_runtime.local_state.queues import PendingEvidence, PendingReport
from edge_runtime.supervisor.evidence import clips_for

STEPS = tuple(f"({i}) step {i}" for i in range(1, 6))


def deadline_violation(at: float, since: float) -> Violation:
    return Violation(
        reason=ReasonCode.DEADLINE_EXCEEDED,
        steps=(STEPS[1],),
        evidence=EvidenceSpan.spanning(HostInstant(at), since=HostInstant(since)),
    )


def decision_with(
    *,
    instance_id: int = 1,
    at: float = 160.0,
    violations: tuple[Violation, ...] = (),
    lifecycle: Lifecycle = Lifecycle.STAYS_OPEN,
    verdict: Verdict = Verdict.FAIL,
) -> Decision:
    return Decision(
        instance_id=instance_id,
        verdict=verdict,
        reasons=tuple(sorted({v.reason for v in violations}, key=lambda reason: reason.value)),
        violations=violations,
        lifecycle=lifecycle,
        evidence=EvidenceSpan.at(HostInstant(at)),
    )


class JudgmentEffectsPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = open_local_state(":memory:")
        self.addCleanup(self.state.close)
        self.store = self.state.station(STATION)

    def persist(
        self, decision: Decision, *, margins: EvidenceMargins = MARGINS
    ) -> tuple[PendingEvidence, ...]:
        instance = Instance(
            instance_id=decision.instance_id,
            opened_at=decision.evidence.anchor,
            last_observation_at=decision.evidence.anchor,
        )
        self.store.commit(
            state=opening_state(),
            decisions=(decision,),
            evidence=clips_for(decision, margins=margins),
            closed_instances=(instance,),
            report_provenance={},
        )
        self.assertEqual(self.store.pending_reports(), (PendingReport(1, decision, 0, None),))
        return self.store.pending_evidence()

    def assert_clips(
        self, clips: tuple[PendingEvidence, ...], expected: tuple[EvidenceClip, ...]
    ) -> None:
        self.assertEqual(
            clips,
            tuple(
                PendingEvidence(i, clip.instance_id, clip.anchor, clip.start, clip.end, 0, None)
                for i, clip in enumerate(expected, start=1)
            ),
        )

    def test_a_passing_close_records_the_decision_clips_and_closes(self) -> None:
        decision = decision_with(
            instance_id=7,
            at=100.0,
            verdict=Verdict.PASS,
            lifecycle=Lifecycle.CLOSED_BY_COMPLETE_SET,
        )
        self.assert_clips(
            self.persist(decision),
            (EvidenceClip(7, HostInstant(100.0), HostInstant(95.0), HostInstant(105.0)),),
        )
        opening = opening_state()
        resumed = self.store.resume(opening.template, opening.parameters)
        self.assertIsNone(resumed.instance)
        self.assertEqual(resumed.next_instance_id, 8)

    def test_each_violation_is_latched_with_its_decision(self) -> None:
        anchor = EvidenceSpan.at(HostInstant(50.0))
        violations = (
            Violation(reason=ReasonCode.WRONG_STEP, steps=(STEPS[3],), evidence=anchor),
            Violation(reason=ReasonCode.MISSED_STEP, steps=(STEPS[1],), evidence=anchor),
        )
        self.persist(decision_with(instance_id=3, at=50.0, violations=violations))
        self.assertEqual(self.store.latched_violations(instance_id=3), violations)

    def test_a_decision_that_stays_open_does_not_close_the_instance(self) -> None:
        self.persist(decision_with(instance_id=3, at=50.0))
        opening = opening_state()
        self.assertEqual(
            self.store.resume(opening.template, opening.parameters).instance,
            Instance(
                instance_id=3, opened_at=HostInstant(50.0), last_observation_at=HostInstant(50.0)
            ),
        )

    def test_the_decision_is_recorded_with_every_latch_and_clip(self) -> None:
        decision = decision_with(instance_id=3, violations=(deadline_violation(160.0, 100.0),))
        clips = self.persist(decision)
        self.assertEqual(self.store.latched_violations(instance_id=3), decision.violations)
        self.assert_clips(
            clips, (EvidenceClip(3, HostInstant(160.0), HostInstant(95.0), HostInstant(165.0)),)
        )

    def test_a_required_span_longer_than_the_window_survives_whole(self) -> None:
        self.assert_clips(
            self.persist(decision_with(violations=(deadline_violation(160.0, 100.0),))),
            (EvidenceClip(1, HostInstant(160.0), HostInstant(95.0), HostInstant(165.0)),),
        )

    def test_zero_margins_leave_the_required_span_exactly(self) -> None:
        self.assert_clips(
            self.persist(
                decision_with(violations=(deadline_violation(160.0, 100.0),)),
                margins=EvidenceMargins(leading=0.0, trailing=0.0),
            ),
            (EvidenceClip(1, HostInstant(160.0), HostInstant(100.0), HostInstant(160.0)),),
        )

    def test_a_negative_margin_is_rejected_where_it_is_configured(self) -> None:
        with self.assertRaises(ValueError):
            EvidenceMargins(leading=-1.0, trailing=5.0)

    def test_violations_at_the_decision_anchor_produce_one_clip(self) -> None:
        anchor = EvidenceSpan.at(HostInstant(300.0))
        decision = decision_with(
            instance_id=2,
            at=300.0,
            lifecycle=Lifecycle.CLOSED_BY_IDLE_TIMEOUT,
            violations=(
                Violation(reason=ReasonCode.MISSED_STEP, steps=(STEPS[3],), evidence=anchor),
                Violation(reason=ReasonCode.MISSED_STEP, steps=(STEPS[4],), evidence=anchor),
            ),
        )
        self.assert_clips(
            self.persist(decision),
            (EvidenceClip(2, HostInstant(300.0), HostInstant(295.0), HostInstant(305.0)),),
        )

    def test_a_violation_anchored_elsewhere_gets_its_own_clip(self) -> None:
        decision = decision_with(
            instance_id=2,
            at=300.0,
            lifecycle=Lifecycle.CLOSED_BY_IDLE_TIMEOUT,
            violations=(
                Violation(
                    reason=ReasonCode.MISSED_STEP,
                    steps=(STEPS[3],),
                    evidence=EvidenceSpan.at(HostInstant(300.0)),
                ),
                deadline_violation(200.0, 140.0),
            ),
        )
        self.assert_clips(
            self.persist(decision),
            (
                EvidenceClip(2, HostInstant(300.0), HostInstant(295.0), HostInstant(305.0)),
                EvidenceClip(2, HostInstant(200.0), HostInstant(135.0), HostInstant(205.0)),
            ),
        )


if __name__ == "__main__":
    unittest.main()
