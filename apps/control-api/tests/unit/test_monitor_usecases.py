from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from factory_sop.auth.authorization import AuthorizationRefusedError, Caller
from factory_sop.auth.model import User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.identifiers import new_id
from factory_sop.monitor.errors import MonitorRefusedError
from factory_sop.monitor.model import (
    MirroredDecision,
    MirroredHealth,
    MirroredObservation,
    MirroredSopInstance,
    MirroredViolation,
)
from factory_sop.monitor.usecases import (
    host_liveness,
    list_instances,
    list_observations,
    list_violations,
    mirror_decision,
    mirror_health,
    mirror_observation,
    sse_snapshot,
    sse_snapshot_state,
    sse_stream,
    stream_health_view,
)
from nvsop_contracts import (
    DECISION_REPORT_CONTRACT_VERSION,
    ReportBackendProvenance,
    ReportedDecision,
    ReportedHealth,
    ReportedObservation,
    ReportEvidence,
    ReportViolation,
    reported_decision_to_wire,
)

HOST_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f101")
STATION_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f102")
BACKEND_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f103")
SECOND_BACKEND_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f104")
CONFIGURATION_REVISION = 11
CONFIGURATION_SHA256 = "c" * 64


class MemoryMonitor:
    def __init__(self) -> None:
        self.decisions: dict[str, MirroredDecision] = {}
        self.health: dict[str, MirroredHealth] = {}
        self.instances: dict[str, MirroredSopInstance] = {}
        self.violations: dict[str, MirroredViolation] = {}
        self.observations: dict[str, MirroredObservation] = {}
        self._decision_sequence = 0
        self._health_sequence = 0

    def upsert_decision(self, value: MirroredDecision) -> bool:
        if value.report.event_id in self.decisions:
            return False
        self._decision_sequence += 1
        self.decisions[value.report.event_id] = replace(
            value, stream_sequence=self._decision_sequence
        )
        return True

    def upsert_health(self, value: MirroredHealth) -> bool:
        if value.report.event_id in self.health:
            return False
        self._health_sequence += 1
        self.health[value.report.event_id] = replace(value, stream_sequence=self._health_sequence)
        return True

    def recent_decisions(self, *, limit: int) -> tuple[MirroredDecision, ...]:
        return tuple(self.decisions.values())[:limit]

    def upsert_instance(self, value: MirroredSopInstance) -> bool:
        if value.report.event_id in self.instances:
            return False
        self.instances[value.report.event_id] = value
        return True

    def page_instances(
        self, *, page: int, page_size: int
    ) -> tuple[tuple[MirroredSopInstance, ...], int]:
        values = tuple(self.instances.values())
        start = (page - 1) * page_size
        return values[start : start + page_size], len(values)

    def upsert_violation(self, value: MirroredViolation) -> bool:
        if value.event_id in self.violations:
            return False
        self.violations[value.event_id] = value
        return True

    def page_violations(
        self, *, page: int, page_size: int
    ) -> tuple[tuple[MirroredViolation, ...], int]:
        values = tuple(self.violations.values())
        start = (page - 1) * page_size
        return values[start : start + page_size], len(values)

    def upsert_observation(self, value: MirroredObservation) -> bool:
        if value.report.event_id in self.observations:
            return False
        self.observations[value.report.event_id] = value
        return True

    def page_observations(
        self,
        *,
        page: int,
        page_size: int,
        station_id: str | None = None,
        instance_id: int | None = None,
    ) -> tuple[tuple[MirroredObservation, ...], int]:
        values = tuple(
            value
            for value in self.observations.values()
            if (station_id is None or value.report.station_id == station_id)
            and (instance_id is None or value.report.instance_id == instance_id)
        )
        start = (page - 1) * page_size
        return values[start : start + page_size], len(values)

    def recent_health(self, *, limit: int) -> tuple[MirroredHealth, ...]:
        return tuple(self.health.values())[:limit]

    def health_for_station(self, *, station_id: str) -> tuple[MirroredHealth, ...]:
        return tuple(
            value for value in self.health.values() if value.report.station_id == station_id
        )

    def last_report_at_by_host(self) -> tuple[tuple[str, datetime], ...]:
        latest: dict[str, datetime] = {}
        for host_id, received_at in (
            *((value.report.host_id, value.received_at) for value in self.decisions.values()),
            *((value.report.host_id, value.received_at) for value in self.health.values()),
            *((value.report.host_id, value.received_at) for value in self.instances.values()),
        ):
            current = latest.get(host_id)
            if current is None or received_at > current:
                latest[host_id] = received_at
        return tuple(sorted(latest.items()))

    def decisions_after_sequence(
        self, *, after_sequence: int, limit: int
    ) -> tuple[MirroredDecision, ...]:
        return tuple(
            value
            for value in sorted(self.decisions.values(), key=lambda item: item.stream_sequence or 0)
            if (value.stream_sequence or 0) > after_sequence
        )[:limit]

    def health_after_sequence(
        self, *, after_sequence: int, limit: int
    ) -> tuple[MirroredHealth, ...]:
        return tuple(
            value
            for value in sorted(self.health.values(), key=lambda item: item.stream_sequence or 0)
            if (value.stream_sequence or 0) > after_sequence
        )[:limit]

    def decision_sequence_for_event(self, event_id: str) -> int | None:
        value = self.decisions.get(event_id)
        return None if value is None else value.stream_sequence

    def health_sequence_for_event(self, event_id: str) -> int | None:
        value = self.health.get(event_id)
        return None if value is None else value.stream_sequence

    def read_after_sequences(
        self,
        *,
        decision_sequence: int,
        health_sequence: int,
        limit: int,
    ) -> tuple[tuple[MirroredDecision, ...], tuple[MirroredHealth, ...]]:
        return (
            self.decisions_after_sequence(after_sequence=decision_sequence, limit=limit),
            self.health_after_sequence(after_sequence=health_sequence, limit=limit),
        )

    def wait_for_wakeup(self, *, timeout: float) -> bool:
        del timeout
        return False


def caller(*permissions: Permission) -> Caller:
    return Caller(
        user=User(
            id=new_id(),
            login_name="monitor-viewer",
            display_name="监控查看者",
            password_hash="argon2-encoded",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
        granted=frozenset(permissions),
    )


class HostGateway:
    def authenticate(self, *, host: object, now: datetime) -> None:
        del host, now

    def owns_station(self, *, host_id: UUID, station_id: UUID) -> bool:
        return host_id == HOST_ID and station_id == STATION_ID

    def owns_station_backend(self, *, host_id: UUID, station_id: UUID, backend_id: UUID) -> bool:
        return host_id == HOST_ID and station_id == STATION_ID and backend_id == BACKEND_ID


class ReboundHostGateway(HostGateway):
    def owns_station(self, *, host_id: UUID, station_id: UUID) -> bool:
        del host_id, station_id
        return False

    def owns_station_backend(self, *, host_id: UUID, station_id: UUID, backend_id: UUID) -> bool:
        del host_id, station_id, backend_id
        return False


class HistoricalAssignments:
    def has_historical_station(
        self,
        *,
        host_id: UUID,
        station_id: UUID,
        template_version_id: str | None = None,
        template_sha256: str | None = None,
    ) -> bool:
        if host_id != HOST_ID or station_id != STATION_ID:
            return False
        if template_version_id is None and template_sha256 is None:
            return True
        return template_version_id == "template-a" and template_sha256 == "a" * 64

    def has_historical_assignment(
        self,
        *,
        host_id: UUID,
        station_id: UUID,
        backend_id: UUID,
        template_version_id: str | None,
        template_sha256: str | None,
        model_ids: tuple[str, ...],
    ) -> bool:
        return (
            host_id == HOST_ID
            and station_id == STATION_ID
            and backend_id == BACKEND_ID
            and template_version_id == "template-a"
            and template_sha256 == "a" * 64
            and model_ids == ("model-1",)
        )

    def registered_host_ids(self) -> tuple[UUID, ...]:
        return (HOST_ID,)

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
            and configuration_revision == CONFIGURATION_REVISION
            and configuration_sha256 == CONFIGURATION_SHA256
            and station_id == STATION_ID
            and template_version_id is None
            and template_sha256 is None
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
            and configuration_revision == CONFIGURATION_REVISION
            and configuration_sha256 == CONFIGURATION_SHA256
            and station_id == STATION_ID
            and (backend_id, model_ids)
            in {
                (BACKEND_ID, ("model-1",)),
                (SECOND_BACKEND_ID, ("model-2",)),
            }
            and template_version_id is None
            and template_sha256 is None
        )


class NoHistoricalAssignments(HistoricalAssignments):
    def has_historical_station(
        self,
        *,
        host_id: UUID,
        station_id: UUID,
        template_version_id: str | None = None,
        template_sha256: str | None = None,
    ) -> bool:
        del host_id, station_id, template_version_id, template_sha256
        return False

    def has_historical_assignment(
        self,
        *,
        host_id: UUID,
        station_id: UUID,
        backend_id: UUID,
        template_version_id: str | None,
        template_sha256: str | None,
        model_ids: tuple[str, ...],
    ) -> bool:
        del host_id, station_id, backend_id, template_version_id, template_sha256, model_ids
        return False


def report(event_id: str = "host:event-1") -> ReportedDecision:
    return ReportedDecision(
        event_id=event_id,
        trace_id="trace-1",
        host_id=str(HOST_ID),
        station_id=str(STATION_ID),
        backend_id=str(BACKEND_ID),
        instance_id=7,
        verdict="indeterminate",
        reason_codes=("FUTURE_REASON",),
        violations=(),
        lifecycle="closed",
        evidence=ReportEvidence(None, None, None),
        template_version_id=None,
        template_sha256=None,
        model_ids=("model-1",),
        reported_at="2026-09-13T00:00:00Z",
    )


def violation(reason_code: str = "MISSED_STEP", step: str = "step-2") -> ReportViolation:
    return ReportViolation(
        reason_code=reason_code,
        detail=None,
        step_ids=(step,),
        evidence=ReportEvidence(anchor=12.0, start=11.0, end=12.0),
    )


def failing_report(event_id: str = "host:fail-1") -> ReportedDecision:
    return replace(
        report(event_id),
        verdict="fail",
        reason_codes=("MISSED_STEP",),
        violations=(violation(),),
    )


def historical_report(event_id: str = "host:historical") -> ReportedDecision:
    return replace(
        report(event_id),
        backend_id=None,
        model_ids=(),
        backend_provenance=(ReportBackendProvenance(str(BACKEND_ID), ("model-1",)),),
        configuration_revision=CONFIGURATION_REVISION,
        configuration_sha256=CONFIGURATION_SHA256,
        contract_version=DECISION_REPORT_CONTRACT_VERSION,
    )


def test_historical_assignment_is_used_instead_of_current_topology() -> None:
    class ReboundGateway(HostGateway):
        def owns_station_backend(
            self, *, host_id: UUID, station_id: UUID, backend_id: UUID
        ) -> bool:
            return False

    monitor = MemoryMonitor()
    assert mirror_decision(
        historical_report(),
        received_at=datetime.now(UTC),
        monitor=monitor,
        host_gateway=ReboundGateway(),
        assignment_gateway=HistoricalAssignments(),
    )


def test_historical_assignment_accepts_station_only_when_no_backend_input_contributed() -> None:
    station_only = replace(
        historical_report("host:historical:connector-only"),
        backend_provenance=(),
    )
    assert mirror_decision(
        station_only,
        received_at=datetime.now(UTC),
        monitor=MemoryMonitor(),
        host_gateway=HostGateway(),
        assignment_gateway=HistoricalAssignments(),
    )


def test_historical_assignment_accepts_only_the_actual_non_first_backend_provenance() -> None:
    report_from_second_backend = replace(
        historical_report("host:historical:backend-2"),
        backend_provenance=(ReportBackendProvenance(str(SECOND_BACKEND_ID), ("model-2",)),),
    )
    assert mirror_decision(
        report_from_second_backend,
        received_at=datetime.now(UTC),
        monitor=MemoryMonitor(),
        host_gateway=HostGateway(),
        assignment_gateway=HistoricalAssignments(),
    )

    with pytest.raises(MonitorRefusedError, match="backend provenance"):
        mirror_decision(
            replace(
                report_from_second_backend,
                backend_provenance=(ReportBackendProvenance(str(SECOND_BACKEND_ID), ("model-1",)),),
            ),
            received_at=datetime.now(UTC),
            monitor=MemoryMonitor(),
            host_gateway=HostGateway(),
            assignment_gateway=HistoricalAssignments(),
        )


def test_historical_assignment_rejects_tampered_proof() -> None:
    with pytest.raises(MonitorRefusedError, match="assignment"):
        mirror_decision(
            replace(historical_report(), configuration_sha256="d" * 64),
            received_at=datetime.now(UTC),
            monitor=MemoryMonitor(),
            host_gateway=HostGateway(),
            assignment_gateway=HistoricalAssignments(),
        )


def test_decision_mirror_is_idempotent_and_preserves_unknown_reason() -> None:
    monitor = MemoryMonitor()
    received_at = datetime.now(UTC)
    assert mirror_decision(
        report(),
        received_at=received_at,
        monitor=monitor,
        host_gateway=HostGateway(),
    )
    assert not mirror_decision(
        report(),
        received_at=received_at,
        monitor=monitor,
        host_gateway=HostGateway(),
    )
    assert monitor.decisions["host:event-1"].report == report()


def test_decision_mirror_preserves_the_complete_report_without_rejudging() -> None:
    original = replace(
        report("host:complete"),
        verdict="fail",
        reason_codes=("WRONG_STEP",),
        violations=(
            ReportViolation(
                reason_code="WRONG_STEP",
                detail="step 2 was not the expected action",
                step_ids=("step-2",),
                evidence=ReportEvidence(anchor=12.0, start=11.0, end=12.0),
            ),
        ),
    )
    monitor = MemoryMonitor()

    assert mirror_decision(
        original,
        received_at=datetime.now(UTC),
        monitor=monitor,
        host_gateway=HostGateway(),
    )

    assert monitor.decisions[original.event_id].report == original
    frame = sse_snapshot(monitor, caller=caller(Permission.MONITOR_VIEW))[0]
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload == reported_decision_to_wire(original)


def test_sse_snapshot_preserves_pass_fail_and_indeterminate_verdicts() -> None:
    reports = (
        replace(report("host:pass"), verdict="pass", reason_codes=()),
        replace(report("host:fail"), verdict="fail", reason_codes=("WRONG_STEP",)),
        replace(report("host:indeterminate"), verdict="indeterminate"),
    )
    monitor = MemoryMonitor()
    for value in reports:
        assert mirror_decision(
            value,
            received_at=datetime.now(UTC),
            monitor=monitor,
            host_gateway=HostGateway(),
        )

    actual = {
        json.loads(frame.split("data: ", 1)[1])["event_id"]: json.loads(frame.split("data: ", 1)[1])
        for frame in sse_snapshot(monitor, caller=caller(Permission.MONITOR_VIEW))
    }
    expected = {value.event_id: reported_decision_to_wire(value) for value in reports}
    assert actual == expected


def test_list_instances_rejects_a_caller_without_monitor_permission() -> None:
    with pytest.raises(AuthorizationRefusedError):
        list_instances(MemoryMonitor(), caller=caller(), page=1, page_size=50)


def test_sse_usecase_rejects_a_caller_without_monitor_permission() -> None:
    with pytest.raises(AuthorizationRefusedError):
        sse_snapshot(MemoryMonitor(), caller=caller())


def test_health_mirror_rejects_a_station_without_current_or_historical_assignment() -> None:
    monitor = MemoryMonitor()
    health = ReportedHealth(
        event_id="host:health-1",
        trace_id="trace-health-1",
        host_id=str(HOST_ID),
        station_id=str(UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f199")),
        stream_id="camera-main",
        status="unavailable",
        reason_code="STREAM_LOST",
        detail="stream unavailable",
        occurred_at="2026-09-13T00:00:00Z",
        source_anchor=None,
        anchor_offset=None,
        reported_at="2026-09-13T00:00:00Z",
    )
    with pytest.raises(MonitorRefusedError, match="assignment"):
        mirror_health(
            health,
            received_at=datetime.now(UTC),
            monitor=monitor,
            host_gateway=HostGateway(),
            device_gateway=HistoricalAssignments(),
        )


def test_health_mirror_accepts_current_assignment_without_history() -> None:
    report_value = ReportedHealth(
        event_id="host:health-current",
        trace_id="trace-health-current",
        host_id=str(HOST_ID),
        station_id=str(STATION_ID),
        stream_id="camera-main",
        status="delivering",
        reason_code=None,
        detail=None,
        occurred_at="2026-09-13T00:00:00Z",
        source_anchor=None,
        anchor_offset=None,
        reported_at="2026-09-13T00:00:01Z",
    )
    assert mirror_health(
        report_value,
        received_at=datetime(2026, 9, 13, 0, 0, 1, tzinfo=UTC),
        monitor=MemoryMonitor(),
        host_gateway=HostGateway(),
        device_gateway=NoHistoricalAssignments(),
    )


def test_health_mirror_accepts_a_historical_assignment_after_topology_change() -> None:
    report_value = ReportedHealth(
        event_id="host:health-historical",
        trace_id="trace-health-historical",
        host_id=str(HOST_ID),
        station_id=str(STATION_ID),
        stream_id="camera-main",
        status="delivering",
        reason_code=None,
        detail=None,
        occurred_at="2026-09-13T00:00:00Z",
        source_anchor=None,
        anchor_offset=None,
        reported_at="2026-09-13T01:00:00Z",
    )
    monitor = MemoryMonitor()

    assert mirror_health(
        report_value,
        received_at=datetime(2026, 9, 13, 1, 0, tzinfo=UTC),
        monitor=monitor,
        host_gateway=ReboundHostGateway(),
        device_gateway=HistoricalAssignments(),
    )


def test_sse_snapshot_contains_event_id_and_raw_reason() -> None:
    monitor = MemoryMonitor()
    mirror_decision(
        report(),
        received_at=datetime.now(UTC),
        monitor=monitor,
        host_gateway=HostGateway(),
    )
    frames = sse_snapshot(monitor, caller=caller(Permission.MONITOR_VIEW))
    assert len(frames) == 1
    assert "id: host:event-1" in frames[0]
    assert '"FUTURE_REASON"' in frames[0]


def test_sse_resume_consumes_last_event_id_without_replaying_it() -> None:
    monitor = MemoryMonitor()
    mirror_decision(
        report("host:event-1"),
        received_at=datetime.now(UTC),
        monitor=monitor,
        host_gateway=HostGateway(),
    )

    snapshot = sse_snapshot_state(
        monitor,
        caller=caller(Permission.MONITOR_VIEW),
        last_event_id="host:event-1",
    )

    assert snapshot.frames == ()
    assert snapshot.decision_sequence == 1


def test_sse_resume_replays_backlog_in_cursor_order_without_snapshot_gap() -> None:
    monitor = MemoryMonitor()
    received_at = datetime(2026, 9, 13, 0, 0, 0, tzinfo=UTC)
    for index in range(1, 106):
        mirror_decision(
            report(f"host:event-{index}"),
            received_at=received_at,
            monitor=monitor,
            host_gateway=HostGateway(),
        )

    snapshot = sse_snapshot_state(
        monitor,
        caller=caller(Permission.MONITOR_VIEW),
        last_event_id="host:event-1",
        limit=100,
    )

    assert len(snapshot.frames) == 100
    assert "id: host:event-2" in snapshot.frames[0]
    assert "id: host:event-101" in snapshot.frames[-1]
    assert snapshot.decision_sequence == 101

    stream = sse_stream(
        monitor,
        caller=caller(Permission.MONITOR_VIEW),
        decision_sequence=snapshot.decision_sequence,
        health_sequence=snapshot.health_sequence,
        wait_timeout=0,
    )
    assert "id: host:event-102" in next(stream)


def test_sse_preserves_stream_sequence_when_received_at_order_reverses() -> None:
    monitor = MemoryMonitor()
    earlier = datetime(2026, 9, 13, 0, 0, 0, tzinfo=UTC)
    later = datetime(2026, 9, 13, 0, 0, 1, tzinfo=UTC)
    mirror_decision(
        report("host:event-1"),
        received_at=later,
        monitor=monitor,
        host_gateway=HostGateway(),
    )
    mirror_decision(
        report("host:event-2"),
        received_at=earlier,
        monitor=monitor,
        host_gateway=HostGateway(),
    )

    snapshot = sse_snapshot_state(
        monitor,
        caller=caller(Permission.MONITOR_VIEW),
        boundary=datetime(2026, 9, 12, tzinfo=UTC),
    )
    assert "id: host:event-1" in snapshot.frames[0]
    assert "id: host:event-2" in snapshot.frames[1]

    stream = sse_stream(
        monitor,
        caller=caller(Permission.MONITOR_VIEW),
        wait_timeout=0,
    )
    assert "id: host:event-1" in next(stream)
    assert "id: host:event-2" in next(stream)


def test_sse_cursors_do_not_replay_snapshot_or_skip_same_timestamp_events() -> None:
    monitor = MemoryMonitor()
    received_at = datetime(2026, 9, 13, 0, 0, 0, tzinfo=UTC)
    mirror_decision(
        report("host:event-1"),
        received_at=received_at,
        monitor=monitor,
        host_gateway=HostGateway(),
    )
    snapshot = sse_snapshot_state(
        monitor,
        caller=caller(Permission.MONITOR_VIEW),
        boundary=datetime(2026, 9, 12, tzinfo=UTC),
    )
    assert snapshot.decision_event_id == "host:event-1"

    mirror_decision(
        report("host:event-2"),
        received_at=received_at,
        monitor=monitor,
        host_gateway=HostGateway(),
    )
    stream = sse_stream(
        monitor,
        caller=caller(Permission.MONITOR_VIEW),
        decision_sequence=snapshot.decision_sequence,
        health_sequence=snapshot.health_sequence,
        wait_timeout=0,
    )
    frame = next(stream)
    assert "id: host:event-2" in frame
    assert "host:event-1" not in frame


def test_decision_mirror_archives_latched_violations_idempotently() -> None:
    monitor = MemoryMonitor()
    received_at = datetime(2026, 9, 13, 0, 0, 0, tzinfo=UTC)
    original = failing_report()

    assert mirror_decision(
        original,
        received_at=received_at,
        monitor=monitor,
        host_gateway=HostGateway(),
    )
    assert not mirror_decision(
        original,
        received_at=datetime(2026, 9, 13, 1, 0, 0, tzinfo=UTC),
        monitor=monitor,
        host_gateway=HostGateway(),
    )

    assert set(monitor.violations) == {"host:fail-1#0"}
    archived = monitor.violations["host:fail-1#0"]
    assert archived.event_id == "host:fail-1#0"
    assert archived.decision_event_id == "host:fail-1"
    assert archived.host_id == str(HOST_ID)
    assert archived.station_id == str(STATION_ID)
    assert archived.instance_id == 7
    assert archived.report == violation()
    # 原发生时刻停留在首次归档, 不被重报时的接收时刻改写 (§AC2)
    assert archived.decision_reported_at == original.reported_at
    assert archived.received_at == received_at


def test_violation_query_retains_instance_and_source_for_the_authorized_caller() -> None:
    monitor = MemoryMonitor()
    mirror_decision(
        failing_report(),
        received_at=datetime(2026, 9, 13, 0, 0, 0, tzinfo=UTC),
        monitor=monitor,
        host_gateway=HostGateway(),
    )

    items, total = list_violations(
        monitor, caller=caller(Permission.MONITOR_VIEW), page=1, page_size=50
    )

    assert total == 1
    assert items[0].to_wire() == {
        "event_id": "host:fail-1#0",
        "decision_event_id": "host:fail-1",
        "host_id": str(HOST_ID),
        "station_id": str(STATION_ID),
        "instance_id": 7,
        "reported_at": "2026-09-13T00:00:00Z",
        "received_at": "2026-09-13T00:00:00+00:00",
        "violation": violation().to_wire(),
    }


def test_list_violations_rejects_a_caller_without_monitor_permission() -> None:
    with pytest.raises(AuthorizationRefusedError):
        list_violations(MemoryMonitor(), caller=caller(), page=1, page_size=50)


def test_indeterminate_decision_does_not_remove_an_existing_violation() -> None:
    monitor = MemoryMonitor()
    mirror_decision(
        failing_report(),
        received_at=datetime(2026, 9, 13, 0, 0, 0, tzinfo=UTC),
        monitor=monitor,
        host_gateway=HostGateway(),
    )

    # 同一实例随后整体不可判定, 既有已锁存违规必须保留 (§5.2, §AC3)
    mirror_decision(
        report("host:fail-1-later"),
        received_at=datetime(2026, 9, 13, 0, 5, 0, tzinfo=UTC),
        monitor=monitor,
        host_gateway=HostGateway(),
    )

    assert set(monitor.violations) == {"host:fail-1#0"}


def observation(
    event_id: str = "host:observation:1", *, instance_id: int = 7
) -> ReportedObservation:
    return ReportedObservation(
        event_id=event_id,
        trace_id=event_id,
        host_id=str(HOST_ID),
        station_id=str(STATION_ID),
        instance_id=instance_id,
        source="action",
        signal="(1) step 1",
        source_time=12.5,
        source_anchor=100.0,
        observed_at=7.5,
        template_version_id="template-a",
        template_sha256="a" * 64,
        backend=ReportBackendProvenance(str(BACKEND_ID), ("model-1",)),
        reported_at="2026-09-13T00:00:00Z",
    )


def test_observation_mirror_is_idempotent_and_queryable_by_station_and_instance() -> None:
    monitor = MemoryMonitor()
    received_at = datetime(2026, 9, 13, 0, 0, 0, tzinfo=UTC)

    assert mirror_observation(
        observation(),
        received_at=received_at,
        monitor=monitor,
        host_gateway=HostGateway(),
        device_gateway=NoHistoricalAssignments(),
    )
    assert not mirror_observation(
        observation(),
        received_at=received_at,
        monitor=monitor,
        host_gateway=HostGateway(),
        device_gateway=NoHistoricalAssignments(),
    )

    items, total = list_observations(
        monitor,
        caller=caller(Permission.MONITOR_VIEW),
        page=1,
        page_size=50,
        station_id=STATION_ID,
        instance_id=7,
    )
    assert total == 1
    assert items[0].report == observation()

    other, nothing = list_observations(
        monitor,
        caller=caller(Permission.MONITOR_VIEW),
        page=1,
        page_size=50,
        station_id=STATION_ID,
        instance_id=8,
    )
    assert (other, nothing) == ((), 0)


def test_observation_mirror_accepts_historical_assignment_after_topology_change() -> None:
    assert mirror_observation(
        observation("host:observation:historical"),
        received_at=datetime(2026, 9, 13, 1, 0, 0, tzinfo=UTC),
        monitor=MemoryMonitor(),
        host_gateway=ReboundHostGateway(),
        device_gateway=HistoricalAssignments(),
    )


def test_observation_mirror_rejects_a_station_without_matching_history() -> None:
    with pytest.raises(MonitorRefusedError):
        mirror_observation(
            replace(observation(), station_id=str(SECOND_BACKEND_ID)),
            received_at=datetime(2026, 9, 13, 0, 0, 0, tzinfo=UTC),
            monitor=MemoryMonitor(),
            host_gateway=HostGateway(),
            device_gateway=HistoricalAssignments(),
        )


def test_list_observations_rejects_a_caller_without_monitor_permission() -> None:
    with pytest.raises(AuthorizationRefusedError):
        list_observations(MemoryMonitor(), caller=caller(), page=1, page_size=50)


def health(
    event_id: str,
    *,
    stream_id: str | None,
    status: str,
    received_at: datetime,
    occurred_at: str = "2026-09-13T00:00:00Z",
) -> MirroredHealth:
    return MirroredHealth(
        report=ReportedHealth(
            event_id=event_id,
            trace_id=event_id,
            host_id=str(HOST_ID),
            station_id=str(STATION_ID),
            stream_id=stream_id,
            status=status,
            reason_code=None,
            detail=None,
            occurred_at=occurred_at,
            source_anchor=None,
            anchor_offset=None,
            reported_at=occurred_at,
        ),
        received_at=received_at,
    )


def test_stream_health_view_reports_no_runtime_data_without_facts() -> None:
    view = stream_health_view(
        MemoryMonitor(), caller=caller(Permission.MONITOR_VIEW), station_id=str(STATION_ID)
    )
    assert view.validity == "no_data"
    assert view.streams == ()


def test_stream_health_view_keeps_latest_per_stream_and_classifies() -> None:
    monitor = MemoryMonitor()
    monitor.upsert_health(
        health(
            "h1",
            stream_id="cam-a",
            status="source_error",
            received_at=datetime(2026, 9, 13, 0, 0, tzinfo=UTC),
            occurred_at="2026-09-13T00:00:00Z",
        )
    )
    monitor.upsert_health(
        health(
            "h2",
            stream_id="cam-a",
            status="delivering",
            received_at=datetime(2026, 9, 13, 0, 1, tzinfo=UTC),
            occurred_at="2026-09-13T00:01:00Z",
        )
    )
    monitor.upsert_health(
        health(
            "h3",
            stream_id="cam-b",
            status="inference_timeout",
            received_at=datetime(2026, 9, 13, 0, 2, tzinfo=UTC),
        )
    )
    view = stream_health_view(
        monitor, caller=caller(Permission.MONITOR_VIEW), station_id=str(STATION_ID)
    )
    assert view.validity == "impaired"
    assert {item.report.stream_id: item.report.status for item in view.streams} == {
        "cam-a": "delivering",
        "cam-b": "inference_timeout",
    }


def test_stream_health_view_ignores_delayed_retry_of_older_event_time() -> None:
    monitor = MemoryMonitor()
    monitor.upsert_health(
        health(
            "newer",
            stream_id="cam-a",
            status="delivering",
            received_at=datetime(2026, 9, 13, 0, 1, tzinfo=UTC),
            occurred_at="2026-09-13T00:01:00Z",
        )
    )
    monitor.upsert_health(
        health(
            "older-retry",
            stream_id="cam-a",
            status="source_error",
            received_at=datetime(2026, 9, 13, 0, 2, tzinfo=UTC),
            occurred_at="2026-09-13T00:00:00Z",
        )
    )

    view = stream_health_view(
        monitor, caller=caller(Permission.MONITOR_VIEW), station_id=str(STATION_ID)
    )
    assert view.validity == "healthy"
    assert [item.report.event_id for item in view.streams] == ["newer"]


def test_stream_health_display_limit_does_not_change_station_validity() -> None:
    monitor = MemoryMonitor()
    monitor.upsert_health(
        health(
            "healthy-newer",
            stream_id="cam-a",
            status="delivering",
            received_at=datetime(2026, 9, 13, 0, 2, tzinfo=UTC),
            occurred_at="2026-09-13T00:02:00Z",
        )
    )
    monitor.upsert_health(
        health(
            "impaired-older",
            stream_id="cam-b",
            status="inference_timeout",
            received_at=datetime(2026, 9, 13, 0, 1, tzinfo=UTC),
            occurred_at="2026-09-13T00:01:00Z",
        )
    )

    view = stream_health_view(
        monitor,
        caller=caller(Permission.MONITOR_VIEW),
        station_id=str(STATION_ID),
        limit=1,
    )
    assert view.validity == "impaired"
    assert [item.report.event_id for item in view.streams] == ["healthy-newer"]


def test_stream_health_view_rejects_a_caller_without_monitor_permission() -> None:
    with pytest.raises(AuthorizationRefusedError):
        stream_health_view(MemoryMonitor(), caller=caller(), station_id=str(STATION_ID))


def test_host_liveness_includes_a_registered_host_that_never_reported() -> None:
    values = host_liveness(
        MemoryMonitor(),
        device_gateway=HistoricalAssignments(),
        caller=caller(Permission.MONITOR_VIEW),
        now=datetime(2026, 9, 13, 0, 10, tzinfo=UTC),
        stale_after_seconds=300.0,
    )
    assert len(values) == 1
    assert values[0].host_id == str(HOST_ID)
    assert values[0].last_reported_at is None
    assert values[0].age_seconds is None
    assert values[0].suspicious is True


def test_host_liveness_marks_a_silent_host_suspicious_without_faking_health() -> None:
    monitor = MemoryMonitor()
    monitor.upsert_health(
        health(
            "h1",
            stream_id="cam-a",
            status="delivering",
            received_at=datetime(2026, 9, 13, 0, 0, tzinfo=UTC),
        )
    )
    values = host_liveness(
        monitor,
        device_gateway=HistoricalAssignments(),
        caller=caller(Permission.MONITOR_VIEW),
        now=datetime(2026, 9, 13, 0, 10, tzinfo=UTC),
        stale_after_seconds=300.0,
    )
    assert len(values) == 1
    assert values[0].host_id == str(HOST_ID)
    assert values[0].age_seconds == 600.0
    assert values[0].suspicious is True


def test_host_liveness_treats_a_recent_report_as_alive() -> None:
    monitor = MemoryMonitor()
    monitor.upsert_health(
        health(
            "h1",
            stream_id="cam-a",
            status="delivering",
            received_at=datetime(2026, 9, 13, 0, 9, tzinfo=UTC),
        )
    )
    values = host_liveness(
        monitor,
        device_gateway=HistoricalAssignments(),
        caller=caller(Permission.MONITOR_VIEW),
        now=datetime(2026, 9, 13, 0, 10, tzinfo=UTC),
        stale_after_seconds=300.0,
    )
    assert values[0].suspicious is False


def test_host_liveness_rejects_a_caller_without_monitor_permission() -> None:
    with pytest.raises(AuthorizationRefusedError):
        host_liveness(
            MemoryMonitor(),
            device_gateway=HistoricalAssignments(),
            caller=caller(),
            now=datetime(2026, 9, 13, 0, 10, tzinfo=UTC),
            stale_after_seconds=300.0,
        )
