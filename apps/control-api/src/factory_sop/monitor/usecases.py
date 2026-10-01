"""monitor 镜像用例和只读看板 SSE 投影。"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.device.api import DeviceMonitorGateway
from factory_sop.monitor.api import HistoricalAssignmentGateway, HostOwnershipGateway
from factory_sop.monitor.errors import MonitorRefusedError
from factory_sop.monitor.model import (
    MirroredDecision,
    MirroredHealth,
    MirroredObservation,
    MirroredSopInstance,
    MirroredViolation,
)
from factory_sop.monitor.repository import MonitorRepository, MonitorStreamSource
from nvsop_contracts import (
    DECISION_REPORT_CONTRACT_VERSION,
    ReportedDecision,
    ReportedDisposal,
    ReportedHealth,
    ReportedObservation,
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
    inserted = monitor.upsert_decision(MirroredDecision(report=report, received_at=received_at))
    _archive_violations(report, received_at=received_at, monitor=monitor)
    return inserted


def _archive_violations(
    report: ReportedDecision,
    *,
    received_at: datetime,
    monitor: MonitorRepository,
) -> None:
    """把判定随附的已锁存违规投影为独立归档；只保存事实，不重新判定。"""
    for index, violation in enumerate(report.violations):
        monitor.upsert_violation(
            MirroredViolation(
                event_id=f"{report.event_id}#{index}",
                decision_event_id=report.event_id,
                host_id=report.host_id,
                station_id=report.station_id,
                instance_id=report.instance_id,
                report=violation,
                decision_reported_at=report.reported_at,
                received_at=received_at,
            )
        )


def mirror_health(
    report: ReportedHealth,
    *,
    received_at: datetime,
    monitor: MonitorRepository,
    host_gateway: HostOwnershipGateway,
    device_gateway: DeviceMonitorGateway,
) -> bool:
    """保存当前归属或不可变历史归属能够证明的一路流健康事实。"""
    _health_occurred_at(report)
    host_id = _uuid(report.host_id, "health host_id")
    station_id = _uuid(report.station_id, "health station_id")
    if not host_gateway.owns_station(
        host_id=host_id, station_id=station_id
    ) and not device_gateway.has_historical_station(host_id=host_id, station_id=station_id):
        raise MonitorRefusedError(
            "reported health is outside current and historical host assignment"
        )
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


def list_instances(
    monitor: MonitorRepository,
    *,
    caller: Caller,
    page: int,
    page_size: int,
) -> tuple[tuple[MirroredSopInstance, ...], int]:
    """返回授权用户可查看的一页 edge 实例生命周期镜像及总数。"""
    authorize(caller, Permission.MONITOR_VIEW)
    return monitor.page_instances(page=page, page_size=page_size)


def list_violations(
    monitor: MonitorRepository,
    *,
    caller: Caller,
    page: int,
    page_size: int,
) -> tuple[tuple[MirroredViolation, ...], int]:
    """返回授权用户可查看的一页已锁存违规归档及总数；保留原实例与来源。"""
    authorize(caller, Permission.MONITOR_VIEW)
    return monitor.page_violations(page=page, page_size=page_size)


def mirror_disposal(
    report: ReportedDisposal,
    *,
    received_at: datetime,
    monitor: MonitorRepository,
    host_gateway: HostOwnershipGateway,
    device_gateway: DeviceMonitorGateway,
) -> bool:
    host_id, station_id = (
        _uuid(report.host_id, "disposal host_id"),
        _uuid(report.station_id, "disposal station_id"),
    )
    if not host_gateway.owns_station(
        host_id=host_id, station_id=station_id
    ) and not device_gateway.has_historical_station(host_id=host_id, station_id=station_id):
        raise MonitorRefusedError(
            "reported disposal is outside current and historical host assignment"
        )
    return monitor.upsert_disposal(report, received_at=received_at)


def list_disposals(
    monitor: MonitorRepository, *, caller: Caller, page: int, page_size: int
) -> tuple[tuple[ReportedDisposal, ...], int]:
    authorize(caller, Permission.MONITOR_VIEW)
    return monitor.page_disposals(page=page, page_size=page_size)


def mirror_observation(
    report: ReportedObservation,
    *,
    received_at: datetime,
    monitor: MonitorRepository,
    host_gateway: HostOwnershipGateway,
    device_gateway: DeviceMonitorGateway,
) -> bool:
    """保存当前归属或产生时 provenance 的历史归属能够证明的观测。"""
    host_id = _uuid(report.host_id, "observation host_id")
    station_id = _uuid(report.station_id, "observation station_id")
    if host_gateway.owns_station(host_id=host_id, station_id=station_id):
        return monitor.upsert_observation(
            MirroredObservation(report=report, received_at=received_at)
        )
    backend = report.backend
    if backend is None:
        assigned = device_gateway.has_historical_station(
            host_id=host_id,
            station_id=station_id,
            template_version_id=report.template_version_id,
            template_sha256=report.template_sha256,
        )
    else:
        assigned = device_gateway.has_historical_assignment(
            host_id=host_id,
            station_id=station_id,
            backend_id=_uuid(backend.backend_id, "observation backend_id"),
            template_version_id=report.template_version_id,
            template_sha256=report.template_sha256,
            model_ids=backend.model_ids,
        )
    if not assigned:
        raise MonitorRefusedError(
            "reported observation is outside current and historical assignment"
        )
    return monitor.upsert_observation(MirroredObservation(report=report, received_at=received_at))


def list_observations(
    monitor: MonitorRepository,
    *,
    caller: Caller,
    page: int,
    page_size: int,
    station_id: UUID | None = None,
    instance_id: int | None = None,
) -> tuple[tuple[MirroredObservation, ...], int]:
    """返回授权用户可按工位/实例定位的一页产生时观测镜像及总数。"""
    authorize(caller, Permission.MONITOR_VIEW)
    return monitor.page_observations(
        page=page,
        page_size=page_size,
        station_id=None if station_id is None else str(station_id),
        instance_id=instance_id,
    )


@dataclass(frozen=True, slots=True)
class StreamHealthView:
    """一个工位的运行有效性投影：每路流的最新事实与工位级分类。

    它只陈述"这段时间的观测能不能用于判定"；配置验证与主机可达性属于 device，不在此重复。
    """

    station_id: str
    validity: str
    streams: tuple[MirroredHealth, ...]


def stream_health_view(
    monitor: MonitorRepository,
    *,
    caller: Caller,
    station_id: str,
    limit: int = 50,
) -> StreamHealthView:
    """返回授权用户可查看的工位运行有效性；不反写 edge，也不虚构健康。

    ``no_data`` 表示中心尚无该工位任何流健康事实；这不是"健康"，也不是"失联"。
    """
    authorize(caller, Permission.MONITOR_VIEW)
    reports = monitor.health_for_station(station_id=station_id)
    latest = latest_health_per_stream(reports)
    if not latest:
        return StreamHealthView(station_id=station_id, validity="no_data", streams=())
    validity = (
        "healthy"
        if all(_health_is_healthy(report.report.status) for report in latest)
        else "impaired"
    )
    ordered = tuple(sorted(latest, key=_health_event_order, reverse=True))
    return StreamHealthView(station_id=station_id, validity=validity, streams=ordered[:limit])


def latest_health_per_stream(reports: tuple[MirroredHealth, ...]) -> tuple[MirroredHealth, ...]:
    """每路流按 edge 事件时间保留最新事实；Center ingestion sequence 只服务 SSE。

    monitor owner 唯一的健康业务分类：SSE 投影与工位视图都复用它，不另造排序。
    """
    latest: dict[str | None, MirroredHealth] = {}
    for report in reports:
        key = report.report.stream_id
        current = latest.get(key)
        if current is None or _health_event_order(report) > _health_event_order(current):
            latest[key] = report
    return tuple(latest.values())


def _health_event_order(value: MirroredHealth) -> tuple[float, str]:
    """使用冻结的事件时间锚排序；event_id 仅为相同时刻提供稳定次序。"""
    return _health_occurred_at(value.report), value.report.event_id


def _health_occurred_at(report: ReportedHealth) -> float:
    """验证健康发生时间，并优先用冻结源锚给事件排序。"""
    try:
        occurred_at = datetime.fromisoformat(report.occurred_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("health occurred_at must be an ISO-8601 timestamp") from error
    if occurred_at.tzinfo is None:
        raise ValueError("health occurred_at must include a timezone")
    if report.source_anchor is not None and report.anchor_offset is not None:
        return report.source_anchor + report.anchor_offset
    return occurred_at.timestamp()


def _health_is_healthy(status: str) -> bool:
    """只有明确的"正在投递"算健康；未知状态保守地按受损处理 (§5.21)。"""
    return status == "delivering"


@dataclass(frozen=True, slots=True)
class HostLiveness:
    """中心对一台推理机的独立存活判据（外部证人，§5.7）。

    它判的是"这台机还活着吗"，不是流健康；因此不反写 edge 判定，也不虚构健康。
    """

    host_id: str
    last_reported_at: datetime | None
    age_seconds: float | None
    suspicious: bool


def host_liveness(
    monitor: MonitorRepository,
    *,
    device_gateway: DeviceMonitorGateway,
    caller: Caller,
    now: datetime,
    stale_after_seconds: float,
) -> tuple[HostLiveness, ...]:
    """按 device 登记主机集合和 Center 接收事实计算独立外部见证。"""
    authorize(caller, Permission.MONITOR_VIEW)
    if stale_after_seconds <= 0:
        raise ValueError("host liveness threshold must be positive")
    last_by_host = dict(monitor.last_report_at_by_host())
    values: list[HostLiveness] = []
    for host_id in device_gateway.registered_host_ids():
        last_reported_at = last_by_host.get(str(host_id))
        if last_reported_at is None:
            values.append(
                HostLiveness(
                    host_id=str(host_id),
                    last_reported_at=None,
                    age_seconds=None,
                    suspicious=True,
                )
            )
            continue
        age = (now - last_reported_at).total_seconds()
        values.append(
            HostLiveness(
                host_id=str(host_id),
                last_reported_at=last_reported_at,
                age_seconds=age,
                suspicious=age > stale_after_seconds,
            )
        )
    return tuple(values)


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
    runtime_projection: tuple[dict[str, object], ...] = ()


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
    resume_decision = monitor.decision_sequence_for_event(last_event_id) if last_event_id else None
    resume_health = monitor.health_sequence_for_event(last_event_id) if last_event_id else None
    if resume_decision is None:
        decisions = monitor.recent_decisions(limit=limit)
        decision_sequence = max((value.stream_sequence or 0 for value in decisions), default=0)
    else:
        decisions = monitor.decisions_after_sequence(
            after_sequence=resume_decision,
            limit=limit,
        )
        decision_sequence = max(
            (value.stream_sequence or 0 for value in decisions),
            default=resume_decision,
        )
    if resume_health is None:
        health = monitor.recent_health(limit=limit)
        health_sequence = max((value.stream_sequence or 0 for value in health), default=0)
    else:
        health = monitor.health_after_sequence(
            after_sequence=resume_health,
            limit=limit,
        )
        health_sequence = max(
            (value.stream_sequence or 0 for value in health),
            default=resume_health,
        )

    events = _merge_sse_events(
        (
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
        ),
        (
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
        ),
    )

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
    runtime_projection = monitor.runtime_projection()
    frames = tuple(
        _sse_frame(event=kind, event_id=event_id, data=data)
        for _, kind, event_id, _, data in events
    ) + tuple(_runtime_frame(value) for value in runtime_projection)
    return SseSnapshot(
        frames=frames,
        decision_after=decision_after,
        decision_event_id=decision_event_id,
        health_after=health_after,
        health_event_id=health_event_id,
        decision_sequence=decision_sequence,
        health_sequence=health_sequence,
        runtime_projection=runtime_projection,
    )


def sse_stream(
    source: MonitorStreamSource,
    *,
    caller: Caller,
    decision_sequence: int = 0,
    health_sequence: int = 0,
    runtime_projection: tuple[dict[str, object], ...] = (),
    wait_timeout: float = 15.0,
) -> Iterator[str]:
    """用 durable cursor 重放事实；LISTEN/NOTIFY 仅缩短下一轮读取的等待。"""
    authorize(caller, Permission.MONITOR_VIEW)
    while True:
        decisions, health = source.read_after_sequences(
            decision_sequence=decision_sequence,
            health_sequence=health_sequence,
            limit=100,
        )
        events = _merge_sse_events(
            (
                (
                    value.received_at,
                    "decision",
                    value.report.event_id,
                    value.stream_sequence or 0,
                    reported_decision_to_wire(value.report),
                )
                for value in decisions
            ),
            (
                (
                    value.received_at,
                    "health",
                    value.report.event_id,
                    value.stream_sequence or 0,
                    reported_health_to_wire(value.report),
                )
                for value in health
            ),
        )
        current_runtime = source.read_runtime_projection()
        previous_by_station = {
            cast(str, value["station_id"]): value for value in runtime_projection
        }
        current_by_station = {cast(str, value["station_id"]): value for value in current_runtime}
        for station_id in sorted(current_by_station):
            value = current_by_station[station_id]
            if previous_by_station.get(station_id) != value:
                yield _runtime_frame(value)
        runtime_projection = current_runtime
        if not events:
            if not source.wait_for_wakeup(timeout=wait_timeout):
                yield ": keep-alive\n\n"
            continue
        for _, kind, event_id, sequence, data in events:
            if kind == "decision":
                decision_sequence = max(decision_sequence, sequence)
            else:
                health_sequence = max(health_sequence, sequence)
            yield _sse_frame(event=kind, event_id=event_id, data=data)


SseEvent = tuple[datetime, str, str, int, dict[str, object]]


def _merge_sse_events(
    decision_events: Iterable[SseEvent], health_events: Iterable[SseEvent]
) -> tuple[SseEvent, ...]:
    """跨 stream 按时间交织，但绝不打乱任一 durable sequence。"""
    decisions = sorted(decision_events, key=lambda item: item[3])
    health = sorted(health_events, key=lambda item: item[3])
    merged: list[SseEvent] = []
    decision_index = 0
    health_index = 0
    while decision_index < len(decisions) and health_index < len(health):
        decision = decisions[decision_index]
        health_event = health[health_index]
        if (decision[0], decision[2]) <= (health_event[0], health_event[2]):
            merged.append(decision)
            decision_index += 1
        else:
            merged.append(health_event)
            health_index += 1
    merged.extend(decisions[decision_index:])
    merged.extend(health[health_index:])
    return tuple(merged)


def _snapshot_cursor(values: Iterator[tuple[datetime, str]]) -> tuple[datetime, str] | None:
    return max(values, default=None)


def _runtime_frame(data: dict[str, object]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: runtime\ndata: {payload}\n\n"


def _sse_frame(*, event: str, event_id: str, data: dict[str, object]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event_id}\nevent: {event}\ndata: {payload}\n\n"


def _uuid(value: str, label: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, AttributeError) as error:
        raise MonitorRefusedError(f"{label} is not a valid identity") from error


__all__ = [
    "HostLiveness",
    "SseSnapshot",
    "StreamHealthView",
    "host_liveness",
    "latest_health_per_stream",
    "list_instances",
    "list_observations",
    "list_violations",
    "mirror_decision",
    "mirror_health",
    "mirror_instance",
    "mirror_observation",
    "sse_snapshot",
    "sse_snapshot_state",
    "sse_stream",
    "stream_health_view",
]
