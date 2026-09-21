"""monitor 镜像用例和只读看板 SSE 投影。"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.monitor.api import HistoricalAssignmentGateway, HostOwnershipGateway
from factory_sop.monitor.errors import MonitorRefusedError
from factory_sop.monitor.model import MirroredDecision, MirroredHealth, MirroredSopInstance
from factory_sop.monitor.repository import MonitorRepository
from nvsop_contracts import (
    DECISION_REPORT_CONTRACT_VERSION,
    ReportedDecision,
    ReportedHealth,
    ReportedSopInstance,
    reported_decision_to_wire,
    reported_health_to_wire,
)


def summary(*, caller: Caller, monitor: MonitorRepository) -> dict[str, object]:
    """只返回已持久化的观测，不推断实时或健康状态。"""
    if not caller.holds(Permission.MONITOR_VIEW):
        return {"status": "not_permitted", "data": {}}
    decisions = monitor.recent_decisions(limit=100)
    health = monitor.recent_health(limit=100)
    data = {
        "recent_decisions": len(decisions),
        "recent_health": len(health),
        "runtime_status": "reported_observations_only",
    }
    status = "available" if decisions or health else "no_data"
    return {"status": status, "data": data}


def mirror_decision(
    report: ReportedDecision,
    *,
    received_at: datetime,
    monitor: MonitorRepository,
    host_gateway: HostOwnershipGateway,
    assignment_gateway: HistoricalAssignmentGateway | None = None,
) -> bool:
    """保存推理机的不可变观测，不在中心重新计算结论。"""
    host_id = _uuid(report.host_id, "report host_id")
    station_id = _uuid(report.station_id, "report station_id")
    if report.contract_version != DECISION_REPORT_CONTRACT_VERSION:
        if report.backend_id is None:
            raise MonitorRefusedError("v1 reported decision has no backend identity")
        backend_id = _uuid(report.backend_id, "report backend_id")
        if not host_gateway.owns_station_backend(
            host_id=host_id,
            station_id=station_id,
            backend_id=backend_id,
        ):
            raise MonitorRefusedError(
                "reported decision is outside the authenticated host assignment"
            )
    else:
        configuration_revision = report.configuration_revision
        configuration_sha256 = report.configuration_sha256
        if configuration_revision is None or configuration_sha256 is None:
            raise MonitorRefusedError("reported decision has incomplete configuration proof")
        if assignment_gateway is None:
            raise MonitorRefusedError("historical assignment verifier is unavailable")
        if not assignment_gateway.has_configuration_station(
            host_id=host_id,
            configuration_revision=configuration_revision,
            configuration_sha256=configuration_sha256,
            station_id=station_id,
            template_version_id=report.template_version_id,
            template_sha256=report.template_sha256,
        ):
            raise MonitorRefusedError(
                "reported decision station is outside the historical host assignment"
            )
        for backend in report.backend_provenance:
            if not assignment_gateway.has_configuration_assignment(
                host_id=host_id,
                configuration_revision=configuration_revision,
                configuration_sha256=configuration_sha256,
                station_id=station_id,
                backend_id=_uuid(backend.backend_id, "report backend provenance id"),
                template_version_id=report.template_version_id,
                template_sha256=report.template_sha256,
                model_ids=backend.model_ids,
            ):
                raise MonitorRefusedError(
                    "reported decision backend provenance is outside the historical host assignment"
                )
    return monitor.upsert_decision(MirroredDecision(report=report, received_at=received_at))


def mirror_health(
    report: ReportedHealth,
    *,
    received_at: datetime,
    monitor: MonitorRepository,
    host_gateway: HostOwnershipGateway,
) -> bool:
    """只保存认证主机所属的健康观测。"""
    host_id = _uuid(report.host_id, "health host_id")
    if report.station_id is not None:
        station_id = _uuid(report.station_id, "health station_id")
        if not host_gateway.owns_station(host_id=host_id, station_id=station_id):
            raise MonitorRefusedError("reported health is outside the authenticated host topology")
    return monitor.upsert_health(MirroredHealth(report=report, received_at=received_at))


def mirror_instance(
    report: ReportedSopInstance,
    *,
    received_at: datetime,
    monitor: MonitorRepository,
    assignment_gateway: HistoricalAssignmentGateway,
) -> bool:
    """按事件时配置验证并镜像 edge 实例，不在中心重建生命周期。"""
    host_id = _uuid(report.host_id, "instance host_id")
    station_id = _uuid(report.station_id, "instance station_id")
    if not assignment_gateway.has_configuration_station(
        host_id=host_id,
        configuration_revision=report.configuration_revision,
        configuration_sha256=report.configuration_sha256,
        station_id=station_id,
        template_version_id=report.template_version_id,
        template_sha256=report.template_sha256,
    ):
        raise MonitorRefusedError(
            "reported instance station is outside the historical host assignment"
        )
    for backend in report.backend_provenance:
        if not assignment_gateway.has_configuration_assignment(
            host_id=host_id,
            configuration_revision=report.configuration_revision,
            configuration_sha256=report.configuration_sha256,
            station_id=station_id,
            backend_id=_uuid(backend.backend_id, "instance backend provenance id"),
            template_version_id=report.template_version_id,
            template_sha256=report.template_sha256,
            model_ids=backend.model_ids,
        ):
            raise MonitorRefusedError(
                "reported instance backend provenance is outside the historical host assignment"
            )
    return monitor.upsert_instance(MirroredSopInstance(report=report, received_at=received_at))


def recent_instances(
    monitor: MonitorRepository,
    *,
    caller: Caller,
    limit: int = 100,
) -> tuple[MirroredSopInstance, ...]:
    """返回授权用户可查看的 edge 实例生命周期镜像。"""
    authorize(caller, Permission.MONITOR_VIEW)
    return monitor.recent_instances(limit=limit)


@dataclass(frozen=True, slots=True)
class SseSnapshot:
    """初始帧和两个镜像表各自的数据库高水位。"""

    frames: tuple[str, ...]
    decision_after: datetime
    decision_event_id: str
    health_after: datetime
    health_event_id: str
    decision_sequence: int = 0
    health_sequence: int = 0


def sse_snapshot(
    monitor: MonitorRepository,
    *,
    caller: Caller,
    limit: int = 100,
) -> tuple[str, ...]:
    """返回有限的初始看板帧，供浏览器连接和测试使用。"""
    return sse_snapshot_state(monitor, caller=caller, limit=limit).frames


def sse_snapshot_state(
    monitor: MonitorRepository,
    *,
    caller: Caller,
    limit: int = 100,
    boundary: datetime | None = None,
    last_event_id: str | None = None,
) -> SseSnapshot:
    """读取初始投影并记录数据库序号，避免墙上时钟造成丢事件窗口。"""
    authorize(caller, Permission.MONITOR_VIEW)
    boundary = boundary or datetime.now(UTC)
    decisions = monitor.recent_decisions(limit=limit)
    health = monitor.recent_health(limit=limit)
    decision_sequence = max((value.stream_sequence or 0 for value in decisions), default=0)
    health_sequence = max((value.stream_sequence or 0 for value in health), default=0)
    resume_decision = monitor.decision_sequence_for_event(last_event_id) if last_event_id else None
    resume_health = monitor.health_sequence_for_event(last_event_id) if last_event_id else None

    events: list[tuple[datetime, str, str, int, dict[str, object]]] = []
    events.extend(
        (
            value.received_at,
            "decision",
            value.report.event_id,
            value.stream_sequence or 0,
            reported_decision_to_wire(value.report),
        )
        for value in decisions
        if resume_decision is None
        or (value.stream_sequence or 0) > resume_decision
        or ((value.stream_sequence or 0) == 0 and value.report.event_id != last_event_id)
    )
    events.extend(
        (
            value.received_at,
            "health",
            value.report.event_id,
            value.stream_sequence or 0,
            reported_health_to_wire(value.report),
        )
        for value in health
        if resume_health is None
        or (value.stream_sequence or 0) > resume_health
        or ((value.stream_sequence or 0) == 0 and value.report.event_id != last_event_id)
    )
    events.sort(key=lambda item: (item[0], item[2]))

    decision_cursor = _snapshot_cursor(
        (value.received_at, value.report.event_id) for value in decisions
    )
    health_cursor = _snapshot_cursor((value.received_at, value.report.event_id) for value in health)
    if decision_cursor is None or decision_cursor[0] <= boundary:
        decision_after, decision_event_id = boundary, ""
    else:
        decision_after, decision_event_id = decision_cursor
    if health_cursor is None or health_cursor[0] <= boundary:
        health_after, health_event_id = boundary, ""
    else:
        health_after, health_event_id = health_cursor
    return SseSnapshot(
        frames=tuple(
            _sse_frame(event=kind, event_id=event_id, data=data)
            for _, kind, event_id, _, data in events
        ),
        decision_after=decision_after,
        decision_event_id=decision_event_id,
        health_after=health_after,
        health_event_id=health_event_id,
        decision_sequence=decision_sequence,
        health_sequence=health_sequence,
    )


def sse_stream(
    monitor: MonitorRepository,
    *,
    caller: Caller,
    after: datetime | None = None,
    sleep: float = 1.0,
    decision_after: datetime | None = None,
    decision_event_id: str = "",
    health_after: datetime | None = None,
    health_event_id: str = "",
    decision_sequence: int = 0,
    health_sequence: int = 0,
) -> Iterator[str]:
    """只轮询中心镜像，并以数据库序号推进两个独立游标。"""
    authorize(caller, Permission.MONITOR_VIEW)
    del after, decision_after, decision_event_id, health_after, health_event_id
    while True:
        decisions = monitor.decisions_after_sequence(
            after_sequence=decision_sequence,
            limit=100,
        )
        health = monitor.health_after_sequence(
            after_sequence=health_sequence,
            limit=100,
        )
        events: list[tuple[datetime, str, str, int, dict[str, object]]] = []
        events.extend(
            (
                value.received_at,
                "decision",
                value.report.event_id,
                value.stream_sequence or 0,
                reported_decision_to_wire(value.report),
            )
            for value in decisions
        )
        events.extend(
            (
                value.received_at,
                "health",
                value.report.event_id,
                value.stream_sequence or 0,
                reported_health_to_wire(value.report),
            )
            for value in health
        )
        events.sort(key=lambda item: (item[0], item[2]))
        if not events:
            yield ": keep-alive\n\n"
            time.sleep(sleep)
            continue
        for _, kind, event_id, sequence, data in events:
            if kind == "decision":
                decision_sequence = max(decision_sequence, sequence)
            else:
                health_sequence = max(health_sequence, sequence)
            yield _sse_frame(event=kind, event_id=event_id, data=data)


def _snapshot_cursor(values: Iterator[tuple[datetime, str]]) -> tuple[datetime, str] | None:
    return max(values, default=None)


def _sse_frame(*, event: str, event_id: str, data: dict[str, object]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event_id}\nevent: {event}\ndata: {payload}\n\n"


def _uuid(value: str, label: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, AttributeError) as error:
        raise MonitorRefusedError(f"{label} is not a valid identity") from error


__all__ = [
    "SseSnapshot",
    "mirror_decision",
    "mirror_health",
    "mirror_instance",
    "recent_instances",
    "sse_snapshot",
    "sse_snapshot_state",
    "sse_stream",
]
