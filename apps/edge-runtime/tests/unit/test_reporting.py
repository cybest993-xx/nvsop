from __future__ import annotations

import unittest

from nvsop_contracts import (
    DECISION_REPORT_CONTRACT_VERSION,
    ReportBackendProvenance,
    ReportedDecision,
    ReportEvidence,
    reported_decision_to_wire,
)

from edge_runtime.judgment.model import Decision, EvidenceSpan, HostInstant, Lifecycle, Violation
from edge_runtime.judgment.reasons import ReasonCode, Verdict
from edge_runtime.local_state.queues import BackendReportContext, PendingReport
from edge_runtime.reporting import (
    ReportContext,
    reported_decision_from_pending,
    reported_instance_from_pending,
)


class ReportingTests(unittest.TestCase):
    def test_closed_instance_reuses_frozen_event_time_provenance(self) -> None:
        pending = PendingReport(
            queue_id=7,
            decision=Decision(
                instance_id=4,
                verdict=Verdict.PASS,
                reasons=(),
                violations=(),
                lifecycle=Lifecycle.CLOSED_BY_COMPLETE_SET,
                evidence=EvidenceSpan.at(HostInstant(9.0)),
            ),
            attempts=0,
            last_error=None,
            opened_at=1.0,
            closed_at=9.0,
            close_reason=Lifecycle.CLOSED_BY_COMPLETE_SET.value,
            context=ReportContext(
                host_id="host-a",
                station_id="station-a",
                backends=(BackendReportContext("backend-a", ("model-a",)),),
                template_version_id="template-a",
                template_sha256="a" * 64,
                configuration_revision=3,
                configuration_sha256="b" * 64,
                configuration_json="{}",
            ),
        )
        report = reported_instance_from_pending(pending, reported_at="now")
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.event_id, "host-a:station-a:instance:4")
        self.assertEqual(report.close_reason, "closed_by_complete_set")
        self.assertEqual(report.backend_provenance[0].model_ids, ("model-a",))

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
            PendingReport(
                queue_id=44,
                decision=decision,
                attempts=0,
                last_error=None,
                context=ReportContext(
                    host_id="host-a",
                    station_id="station-a",
                    backends=(BackendReportContext("backend-a", ("future-model",)),),
                    template_version_id="template-a",
                    template_sha256="a" * 64,
                    configuration_revision=7,
                    configuration_sha256="b" * 64,
                    configuration_json="{}",
                ),
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
                backend_id=None,
                instance_id=9,
                verdict="indeterminate",
                reason_codes=("STREAM_LOST",),
                violations=(),
                lifecycle="closed_by_run_interruption",
                evidence=ReportEvidence(anchor=12.0, start=12.0, end=12.0),
                template_version_id="template-a",
                template_sha256="a" * 64,
                model_ids=(),
                reported_at="2026-09-13T00:00:00Z",
                backend_provenance=(ReportBackendProvenance("backend-a", ("future-model",)),),
                configuration_revision=7,
                configuration_sha256="b" * 64,
                contract_version=DECISION_REPORT_CONTRACT_VERSION,
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
            PendingReport(
                queue_id=2,
                decision=decision,
                attempts=0,
                last_error=None,
                context=ReportContext(
                    host_id="host",
                    station_id="station",
                    backends=(BackendReportContext("backend", ()),),
                    template_version_id=None,
                    template_sha256=None,
                    configuration_revision=3,
                    configuration_sha256="c" * 64,
                    configuration_json="{}",
                ),
            ),
            reported_at="now",
        )
        self.assertEqual(report.violations[0].step_ids, ("step-2",))
        self.assertEqual(report.violations[0].evidence.start, 8.0)

    def test_event_time_context_without_history_uses_current_topology_wire_shape(self) -> None:
        decision = Decision(
            instance_id=2,
            verdict=Verdict.PASS,
            reasons=(),
            violations=(),
            lifecycle=Lifecycle.CLOSED_BY_END_SIGNAL,
            evidence=EvidenceSpan.at(HostInstant(1.0)),
        )
        report = reported_decision_from_pending(
            PendingReport(
                queue_id=5,
                decision=decision,
                attempts=0,
                last_error=None,
                context=ReportContext(
                    host_id="host-bootstrap",
                    station_id="station-bootstrap",
                    backends=(BackendReportContext("backend-bootstrap", ()),),
                    template_version_id=None,
                    template_sha256=None,
                    configuration_revision=None,
                    configuration_sha256=None,
                    configuration_json=None,
                ),
            ),
            reported_at="now",
        )
        wire = reported_decision_to_wire(report)
        self.assertEqual(report.backend_id, "backend-bootstrap")
        self.assertNotIn("configuration_revision", wire)
        self.assertNotIn("configuration_sha256", wire)

    def test_legacy_pending_without_event_time_context_is_not_relabelled(self) -> None:
        decision = Decision(
            instance_id=3,
            verdict=Verdict.PASS,
            reasons=(),
            violations=(),
            lifecycle=Lifecycle.CLOSED_BY_END_SIGNAL,
            evidence=EvidenceSpan.at(HostInstant(1.0)),
        )
        with self.assertRaisesRegex(ValueError, "event-time report context"):
            reported_decision_from_pending(
                PendingReport(queue_id=3, decision=decision, attempts=0, last_error=None),
                reported_at="now",
            )


if __name__ == "__main__":
    unittest.main()
