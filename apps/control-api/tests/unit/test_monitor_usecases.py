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
from factory_sop.monitor.model import MirroredDecision, MirroredHealth
from factory_sop.monitor.usecases import (
    mirror_decision,
    mirror_health,
    sse_snapshot,
    sse_snapshot_state,
    sse_stream,
)
from nvsop_contracts import (
    ReportedDecision,
    ReportedHealth,
    ReportEvidence,
    ReportViolation,
    reported_decision_to_wire,
)

HOST_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f101")
STATION_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f102")
BACKEND_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f103")


class MemoryMonitor:
    def __init__(self) -> None:
        self.decisions: dict[str, MirroredDecision] = {}
        self.health: dict[str, MirroredHealth] = {}
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

    def recent_health(self, *, limit: int) -> tuple[MirroredHealth, ...]:
        return tuple(self.health.values())[:limit]

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


def test_sse_usecase_rejects_a_caller_without_monitor_permission() -> None:
    with pytest.raises(AuthorizationRefusedError):
        sse_snapshot(MemoryMonitor(), caller=caller())


def test_health_mirror_rejects_a_station_outside_the_host_topology() -> None:
    monitor = MemoryMonitor()
    health = ReportedHealth(
        event_id="host:health-1",
        trace_id="trace-health-1",
        host_id=str(HOST_ID),
        station_id=str(UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f199")),
        status="unavailable",
        reason_code="STREAM_LOST",
        detail="stream unavailable",
        reported_at="2026-09-13T00:00:00Z",
    )
    with pytest.raises(MonitorRefusedError, match="outside"):
        mirror_health(
            health,
            received_at=datetime.now(UTC),
            monitor=monitor,
            host_gateway=HostGateway(),
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
        after=snapshot.decision_after,
        decision_event_id=snapshot.decision_event_id,
        health_after=snapshot.health_after,
        health_event_id=snapshot.health_event_id,
        decision_sequence=snapshot.decision_sequence,
        health_sequence=snapshot.health_sequence,
        sleep=0,
    )
    frame = next(stream)
    assert "id: host:event-2" in frame
    assert "host:event-1" not in frame
