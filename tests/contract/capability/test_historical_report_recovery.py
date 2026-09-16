"""跨应用恢复拓扑变化前产生的历史判定。"""

from __future__ import annotations

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
from edge_runtime.local_state import open_local_state
from edge_runtime.local_state.queues import ReportContext
from edge_runtime.reporting import DecisionReporter

from factory_sop.monitor.model import MirroredDecision
from factory_sop.monitor.usecases import mirror_decision
from nvsop_contracts import ReportedDecision

HOST_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f201")
STATION_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f202")
BACKEND_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f203")
TEMPLATE_ID = "019937d8-0d10-7b31-8d2d-4e60c8f4f204"
CONFIGURATION_SHA256 = "c" * 64
TEMPLATE_SHA256 = "d" * 64


class HistoricalAssignmentGateway:
    """当前拓扑已改绑，但修订 7 仍是 Center 已下发的历史事实。"""

    def owns_station_backend(self, *, host_id: UUID, station_id: UUID, backend_id: UUID) -> bool:
        return False

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
            and configuration_sha256 == CONFIGURATION_SHA256
            and station_id == STATION_ID
            and backend_id == BACKEND_ID
            and template_version_id == TEMPLATE_ID
            and template_sha256 == TEMPLATE_SHA256
            and model_ids == ("model-7",)
        )


class MonitorMirror:
    def __init__(self) -> None:
        self.decisions: dict[str, MirroredDecision] = {}

    def upsert_decision(self, value: MirroredDecision) -> bool:
        existing = self.decisions.get(value.report.event_id)
        if existing is not None:
            if existing.report != value.report:
                raise AssertionError("same event id changed payload")
            return False
        self.decisions[value.report.event_id] = value
        return True


class MirrorTransport:
    def __init__(self, monitor: MonitorMirror) -> None:
        self.monitor = monitor
        self.accepted: list[ReportedDecision] = []

    def send_decision(self, report: ReportedDecision) -> None:
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


class HistoricalRecoveryContractTest(unittest.TestCase):
    def test_offline_decision_flushes_after_center_rebind_and_clears_outbox(self) -> None:
        context_n = ReportContext(
            host_id=str(HOST_ID),
            station_id=str(STATION_ID),
            backend_id=str(BACKEND_ID),
            template_version_id=TEMPLATE_ID,
            template_sha256=TEMPLATE_SHA256,
            model_ids=("model-7",),
            configuration_revision=7,
            configuration_sha256=CONFIGURATION_SHA256,
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
        )

        (offline_pending,) = station.pending_reports()
        self.assertEqual(offline_pending.context, context_n)

        monitor = MonitorMirror()
        transport = MirrorTransport(monitor)
        attempts = DecisionReporter(queues=station, transport=transport).flush(
            now=HostInstant(10.0),
            reported_at="2026-09-16T00:00:00Z",
        )

        self.assertEqual(len(attempts), 1)
        self.assertTrue(attempts[0].sent)
        self.assertEqual(len(transport.accepted), 1)
        self.assertEqual(transport.accepted[0].configuration_revision, 7)
        self.assertEqual(transport.accepted[0].backend_id, str(BACKEND_ID))
        self.assertEqual(station.pending_reports(), ())


if __name__ == "__main__":
    unittest.main()
