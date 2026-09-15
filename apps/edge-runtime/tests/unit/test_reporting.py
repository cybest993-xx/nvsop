from __future__ import annotations

import unittest

from nvsop_contracts import ReportedDecision, ReportEvidence

from edge_runtime.judgment.model import Decision, EvidenceSpan, HostInstant, Lifecycle, Violation
from edge_runtime.judgment.reasons import ReasonCode, Verdict
from edge_runtime.local_state.queues import PendingReport
from edge_runtime.reporting import ReportContext, reported_decision_from_pending


class ReportingTests(unittest.TestCase):
    def test_event_and_trace_identity_are_stable_and_unknown_wire_data_is_preserved(self) -> None:
        decision = Decision(
            instance_id=9,
            verdict=Verdict.INDETERMINATE,
            reasons=(ReasonCode.STREAM_LOST,),
            violations=(),
            lifecycle=Lifecycle.CLOSED_BY_RUN_INTERRUPTION,
            evidence=EvidenceSpan.at(HostInstant(12.0)),
        )
        report = reported_decision_from_pending(
            PendingReport(queue_id=44, decision=decision, attempts=0, last_error=None),
            context=ReportContext(
                host_id="host-a",
                station_id="station-a",
                backend_id="backend-a",
                template_version_id="template-a",
                template_sha256="a" * 64,
                model_ids=("future-model",),
            ),
            reported_at="2026-09-13T00:00:00Z",
        )
        self.assertEqual(
            report,
            ReportedDecision(
                event_id="host-a:44",
                trace_id="host-a:44",
                host_id="host-a",
                station_id="station-a",
                backend_id="backend-a",
                instance_id=9,
                verdict="indeterminate",
                reason_codes=("STREAM_LOST",),
                violations=(),
                lifecycle="closed_by_run_interruption",
                evidence=ReportEvidence(anchor=12.0, start=12.0, end=12.0),
                template_version_id="template-a",
                template_sha256="a" * 64,
                model_ids=("future-model",),
                reported_at="2026-09-13T00:00:00Z",
            ),
        )

    def test_violation_steps_and_evidence_are_carried(self) -> None:
        violation = Violation(
            reason=ReasonCode.WRONG_STEP,
            steps=("step-2",),
            evidence=EvidenceSpan.spanning(HostInstant(10), HostInstant(8)),
        )
        decision = Decision(
            instance_id=1,
            verdict=Verdict.FAIL,
            reasons=(ReasonCode.WRONG_STEP,),
            violations=(violation,),
            lifecycle=Lifecycle.CLOSED_BY_END_SIGNAL,
            evidence=EvidenceSpan.at(HostInstant(10)),
        )
        report = reported_decision_from_pending(
            PendingReport(queue_id=2, decision=decision, attempts=0, last_error=None),
            context=ReportContext(
                host_id="host",
                station_id="station",
                backend_id="backend",
                template_version_id=None,
                template_sha256=None,
                model_ids=(),
            ),
            reported_at="now",
        )
        self.assertEqual(report.violations[0].step_ids, ("step-2",))
        self.assertEqual(report.violations[0].evidence.start, 8.0)


if __name__ == "__main__":
    unittest.main()
