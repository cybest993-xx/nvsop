"""跨应用恢复拓扑变化前产生的历史判定。"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from uuid import UUID

from edge_runtime.judgment.model import (
    Decision,
    EvidenceSpan,
    HostInstant,
    Instance,
    JudgmentState,
    Lifecycle,
    Ordering,
    RuntimeParameters,
    Template,
)
from edge_runtime.judgment.reasons import Verdict
from edge_runtime.local_state import BackendReportContext, ReportContext, open_local_state
from edge_runtime.reporting import HostReportReconciler

from factory_sop.monitor.model import MirroredDecision, MirroredSopInstance
from factory_sop.monitor.usecases import mirror_decision, mirror_instance
from nvsop_contracts import (
    ConfigurationBundle,
    ReportBackendProvenance,
    ReportedDecision,
    ReportedSopInstance,
    configuration_to_wire,
)

HOST_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f201")
STATION_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f202")
BACKEND_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f203")
TEMPLATE_ID = "019937d8-0d10-7b31-8d2d-4e60c8f4f204"
TEMPLATE_SHA256 = "d" * 64


class HistoricalAssignmentGateway:
    """当前拓扑已改绑，但修订 7 仍是 Center 已下发的历史事实。"""

    def owns_station_backend(self, *, host_id: UUID, station_id: UUID, backend_id: UUID) -> bool:
        return False

    def has_configuration_station(
        self,
        *,
        host_id: UUID,
        configuration_revision: int,
        configuration_sha256: str,
        station_id: UUID,
        template_version_id: str | None,
        template_sha256: str | None,
    ) -> bool:
        return (
            host_id == HOST_ID
            and configuration_revision == 7
            and station_id == STATION_ID
            and template_version_id == TEMPLATE_ID
            and template_sha256 == TEMPLATE_SHA256
            and bool(configuration_sha256)
        )

    def has_configuration_assignment(
        self,
        *,
        host_id: UUID,
        configuration_revision: int,
        configuration_sha256: str,
        station_id: UUID,
        backend_id: UUID,
        template_version_id: str | None,
        template_sha256: str | None,
        model_ids: tuple[str, ...],
    ) -> bool:
        return (
            host_id == HOST_ID
            and configuration_revision == 7
            and bool(configuration_sha256)
            and station_id == STATION_ID
            and backend_id == BACKEND_ID
            and template_version_id == TEMPLATE_ID
            and template_sha256 == TEMPLATE_SHA256
            and model_ids == ("model-7",)
        )


class MonitorMirror:
    def __init__(self) -> None:
        self.decisions: dict[str, MirroredDecision] = {}
        self.instances: dict[str, MirroredSopInstance] = {}

    def upsert_decision(self, value: MirroredDecision) -> bool:
        existing = self.decisions.get(value.report.event_id)
        if existing is not None:
            if existing.report != value.report:
                raise AssertionError("same event id changed payload")
            return False
        self.decisions[value.report.event_id] = value
        return True

    def upsert_instance(self, value: MirroredSopInstance) -> bool:
        existing = self.instances.get(value.report.event_id)
        if existing is not None:
            if existing.report != value.report:
                raise AssertionError("same instance event id changed payload")
            return False
        self.instances[value.report.event_id] = value
        return True


class MirrorTransport:
    def __init__(self, monitor: MonitorMirror) -> None:
        self.monitor = monitor
        self.accepted: list[ReportedDecision] = []
        self.accepted_instances: list[ReportedSopInstance] = []

    def send_decision(
        self,
        report: ReportedDecision,
        *,
        configuration: ConfigurationBundle | None,
    ) -> None:
        if configuration is None:
            raise AssertionError("historical recovery must keep its frozen configuration")
        inserted = mirror_decision(
            report,
            received_at=datetime(2026, 9, 16, tzinfo=UTC),
            monitor=self.monitor,  # type: ignore[arg-type]
            host_gateway=HistoricalAssignmentGateway(),  # type: ignore[arg-type]
            assignment_gateway=HistoricalAssignmentGateway(),
        )
        if not inserted:
            raise AssertionError("first recovery delivery must insert the mirror")
        self.accepted.append(report)

    def send_instance(
        self,
        report: ReportedSopInstance,
        *,
        configuration: ConfigurationBundle | None,
    ) -> None:
        if configuration is None:
            raise AssertionError("instance recovery must keep its frozen configuration")
        inserted = mirror_instance(
            report,
            received_at=datetime(2026, 9, 16, tzinfo=UTC),
            monitor=self.monitor,  # type: ignore[arg-type]
            assignment_gateway=HistoricalAssignmentGateway(),
        )
        if not inserted:
            raise AssertionError("first instance recovery delivery must insert the mirror")
        self.accepted_instances.append(report)


class HistoricalRecoveryContractTest(unittest.TestCase):
    def test_offline_decision_flushes_after_center_rebind_and_clears_outbox(self) -> None:
        bundle_n = ConfigurationBundle(
            host_id=str(HOST_ID),
            config_revision=7,
            generated_at="2026-09-16T00:00:00Z",
            stations=(),
        )
        provenance_n = BackendReportContext(str(BACKEND_ID), ("model-7",))
        context_n = ReportContext(
            host_id=str(HOST_ID),
            station_id=str(STATION_ID),
            backends=(provenance_n,),
            template_version_id=TEMPLATE_ID,
            template_sha256=TEMPLATE_SHA256,
            configuration_revision=7,
            configuration_sha256=bundle_n.effective_sha256,
            configuration_json=json.dumps(
                configuration_to_wire(bundle_n), ensure_ascii=False, separators=(",", ":")
            ),
        )
        state = open_local_state(":memory:")
        self.addCleanup(state.close)
        station = state.station(str(STATION_ID), report_context=context_n)
        instance = Instance(
            instance_id=1,
            opened_at=HostInstant(1.0),
            last_observation_at=HostInstant(2.0),
        )
        decision = Decision(
            instance_id=1,
            verdict=Verdict.PASS,
            reasons=(),
            violations=(),
            lifecycle=Lifecycle.CLOSED_BY_END_SIGNAL,
            evidence=EvidenceSpan.at(HostInstant(2.0)),
        )
        station.commit(
            state=JudgmentState(
                template=Template(
                    steps=("step-1",),
                    ordering=Ordering.ORDERED,
                    start_signal="step-1",
                ),
                parameters=RuntimeParameters(idle_timeout=30.0, step_deadline=10.0),
                next_instance_id=2,
            ),
            decisions=(decision,),
            evidence=(),
            closed_instances=(instance,),
            report_provenance={1: (provenance_n,)},
        )

        (offline_pending,) = station.pending_reports()
        self.assertEqual(offline_pending.context, context_n)

        monitor = MonitorMirror()
        transport = MirrorTransport(monitor)
        attempts = HostReportReconciler(reports=state.reports(), transport=transport).flush(
            now=HostInstant(10.0),
            reported_at="2026-09-16T00:00:00Z",
        )

        self.assertEqual(len(attempts), 1)
        self.assertTrue(attempts[0].sent)
        self.assertEqual(len(transport.accepted), 1)
        self.assertEqual(transport.accepted[0].configuration_revision, 7)
        self.assertIsNone(transport.accepted[0].backend_id)
        self.assertEqual(
            transport.accepted[0].backend_provenance,
            (ReportBackendProvenance(str(BACKEND_ID), ("model-7",)),),
        )
        self.assertEqual(len(transport.accepted_instances), 1)
        self.assertEqual(transport.accepted_instances[0].opened_at, 1.0)
        self.assertEqual(transport.accepted_instances[0].closed_at, 2.0)
        self.assertEqual(transport.accepted_instances[0].close_reason, "closed_by_end_signal")
        self.assertEqual(station.pending_reports(), ())


if __name__ == "__main__":
    unittest.main()
