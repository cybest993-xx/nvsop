from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from auth_fakes import caller_holding

from factory_sop.auth.authorization import AuthorizationRefusedError
from factory_sop.auth.permissions import Permission
from factory_sop.monitor.errors import MonitorRefusedError
from factory_sop.monitor.model import MirroredDecision, MirroredHealth
from factory_sop.monitor.usecases import (
    authorize_stream_access,
    mirror_decision,
    mirror_health,
    sse_snapshot,
    sse_snapshot_state,
    sse_stream,
)
from nvsop_contracts import ReportedDecision, ReportedHealth, ReportEvidence

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

    def recent_decisions(
        self, *, limit: int, through_sequence: int | None = None
    ) -> tuple[MirroredDecision, ...]:
        values = tuple(
            value
            for value in self.decisions.values()
            if through_sequence is None or (value.stream_sequence or 0) <= through_sequence
        )
        return values[:limit]

    def recent_health(
        self, *, limit: int, through_sequence: int | None = None
    ) -> tuple[MirroredHealth, ...]:
        values = tuple(
            value
            for value in self.health.values()
            if through_sequence is None or (value.stream_sequence or 0) <= through_sequence
        )
        return values[:limit]

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

    def event_cursor_for(self, event_id: str) -> tuple[datetime, str] | None:
        values = [
            value.received_at
            for value in self.decisions.values()
            if value.report.event_id == event_id
        ]
        values.extend(
            value.received_at for value in self.health.values() if value.report.event_id == event_id
        )
        return None if not values else (min(values), event_id)

    def decisions_after_cursor(
        self,
        *,
        after: tuple[datetime, str],
        limit: int | None,
        through_sequence: int | None = None,
    ) -> tuple[MirroredDecision, ...]:
        values = tuple(
            value
            for value in self.decisions.values()
            if (value.received_at, value.report.event_id) > after
            and (through_sequence is None or (value.stream_sequence or 0) <= through_sequence)
        )
        return values if limit is None else values[:limit]

    def health_after_cursor(
        self,
        *,
        after: tuple[datetime, str],
        limit: int | None,
        through_sequence: int | None = None,
    ) -> tuple[MirroredHealth, ...]:
        values = tuple(
            value
            for value in self.health.values()
            if (value.received_at, value.report.event_id) > after
            and (through_sequence is None or (value.stream_sequence or 0) <= through_sequence)
        )
        return values if limit is None else values[:limit]

    def stream_watermarks(self) -> tuple[int, int]:
        return self._decision_sequence, self._health_sequence


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


def health_report(event_id: str = "host:health-1") -> ReportedHealth:
    return ReportedHealth(
        event_id=event_id,
        trace_id="trace-health-1",
        host_id=str(HOST_ID),
        station_id=str(STATION_ID),
        status="available",
        reason_code=None,
        detail=None,
        reported_at="2026-09-13T00:00:00Z",
    )


def test_monitor_stream_authorization_is_owned_by_the_usecase() -> None:
    with pytest.raises(AuthorizationRefusedError):
        authorize_stream_access(caller_holding())

    authorize_stream_access(caller_holding(Permission.MONITOR_VIEW))


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
    assert monitor.decisions["host:event-1"].report.reason_codes == ("FUTURE_REASON",)


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
    frames = sse_snapshot(monitor)
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

    snapshot = sse_snapshot_state(monitor, last_event_id="host:event-1")

    assert snapshot.frames == ()
    assert snapshot.decision_sequence == 1


def test_sse_resume_does_not_replay_the_other_event_kind() -> None:
    monitor = MemoryMonitor()
    mirror_decision(
        report("host:decision-before"),
        received_at=datetime(2026, 9, 13, 0, 0, 1, tzinfo=UTC),
        monitor=monitor,
        host_gateway=HostGateway(),
    )
    mirror_health(
        health_report(),
        received_at=datetime(2026, 9, 13, 0, 0, 2, tzinfo=UTC),
        monitor=monitor,
        host_gateway=HostGateway(),
    )
    mirror_decision(
        report("host:decision-after"),
        received_at=datetime(2026, 9, 13, 0, 0, 3, tzinfo=UTC),
        monitor=monitor,
        host_gateway=HostGateway(),
    )

    snapshot = sse_snapshot_state(monitor, last_event_id="host:health-1")

    assert len(snapshot.frames) == 1
    assert "host:decision-after" in snapshot.frames[0]
    assert "host:decision-before" not in snapshot.frames[0]


def test_sse_resume_replays_the_entire_backlog_after_the_last_event() -> None:
    monitor = MemoryMonitor()
    for index in range(150):
        mirror_decision(
            report(f"host:decision-{index}"),
            received_at=datetime(2026, 9, 13, tzinfo=UTC) + timedelta(seconds=index),
            monitor=monitor,
            host_gateway=HostGateway(),
        )

    snapshot = sse_snapshot_state(monitor, last_event_id="host:decision-0")

    assert len(snapshot.frames) == 149
    assert "host:decision-1" in snapshot.frames[0]
    assert "host:decision-149" in snapshot.frames[-1]
    assert snapshot.decision_sequence == 150


def test_sse_cursors_do_not_replay_snapshot_or_skip_same_timestamp_events() -> None:
    monitor = MemoryMonitor()
    received_at = datetime(2026, 9, 13, 0, 0, 0, tzinfo=UTC)
    mirror_decision(
        report("host:event-1"),
        received_at=received_at,
        monitor=monitor,
        host_gateway=HostGateway(),
    )
    snapshot = sse_snapshot_state(monitor)
    assert snapshot.decision_sequence == 1

    mirror_decision(
        report("host:event-2"),
        received_at=received_at,
        monitor=monitor,
        host_gateway=HostGateway(),
    )
    stream = sse_stream(
        monitor,
        decision_sequence=snapshot.decision_sequence,
        health_sequence=snapshot.health_sequence,
        sleep=0,
    )
    frame = next(stream)
    assert "id: host:event-2" in frame
    assert "host:event-1" not in frame
