"""monitor 镜像用例和只读看板 SSE 投影。"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.monitor.api import HostOwnershipGateway
from factory_sop.monitor.errors import MonitorRefusedError
from factory_sop.monitor.model import MirroredDecision, MirroredHealth
from factory_sop.monitor.repository import MonitorRepository
from nvsop_contracts import (
    ReportedDecision,
    ReportedHealth,
    reported_decision_to_wire,
    reported_health_to_wire,
)


def authorize_stream_access(caller: Caller) -> None:
    """校验 monitor SSE 的查看权限; HTTP 适配器不直接接触授权函数。"""
    authorize(caller, Permission.MONITOR_VIEW)


def summary(*, caller: Caller, monitor: MonitorRepository) -> dict[str, object]:
    """只返回已持久化观测；绝不推断在线或健康状态。"""
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
) -> bool:
    """保存推理机的不可变观测，不在中心重新计算结论。"""
    host_id = _uuid(report.host_id, "report host_id")
    station_id = _uuid(report.station_id, "report station_id")
    backend_id = _uuid(report.backend_id, "report backend_id")
    if not host_gateway.owns_station_backend(
        host_id=host_id,
        station_id=station_id,
        backend_id=backend_id,
    ):
        raise MonitorRefusedError("reported decision is outside the authenticated host topology")
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


@dataclass(frozen=True, slots=True)
class SseSnapshot:
    """初始帧和两个镜像表各自的数据库序号高水位。"""

    frames: tuple[str, ...]
    decision_sequence: int = 0
    health_sequence: int = 0


def sse_snapshot(monitor: MonitorRepository, *, limit: int = 100) -> tuple[str, ...]:
    """返回有限的初始看板帧，供浏览器连接和测试使用。"""
    return sse_snapshot_state(monitor, limit=limit).frames


def sse_snapshot_state(
    monitor: MonitorRepository,
    *,
    limit: int = 100,
    last_event_id: str | None = None,
) -> SseSnapshot:
    """读取初始投影并记录数据库序号，避免用墙上时钟制造丢事件窗口。"""
    resume_cursor = monitor.event_cursor_for(last_event_id) if last_event_id else None
    decision_sequence, health_sequence = monitor.stream_watermarks()
    if resume_cursor is None:
        decisions = monitor.recent_decisions(limit=limit, through_sequence=decision_sequence)
        health = monitor.recent_health(limit=limit, through_sequence=health_sequence)
    else:
        decisions = monitor.decisions_after_cursor(
            after=resume_cursor,
            limit=None,
            through_sequence=decision_sequence,
        )
        health = monitor.health_after_cursor(
            after=resume_cursor,
            limit=None,
            through_sequence=health_sequence,
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
        if resume_cursor is None or (value.received_at, value.report.event_id) > resume_cursor
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
        if resume_cursor is None or (value.received_at, value.report.event_id) > resume_cursor
    )
    events.sort(key=lambda item: (item[0], item[2]))

    return SseSnapshot(
        frames=tuple(
            _sse_frame(event=kind, event_id=event_id, data=data)
            for _, kind, event_id, _, data in events
        ),
        decision_sequence=decision_sequence,
        health_sequence=health_sequence,
    )


def sse_stream(
    monitor: MonitorRepository,
    *,
    sleep: float = 1.0,
    decision_sequence: int = 0,
    health_sequence: int = 0,
) -> Iterator[str]:
    """只轮询中心镜像，并以数据库序号推进两个独立游标。"""
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
    "authorize_stream_access",
    "mirror_decision",
    "mirror_health",
    "sse_snapshot",
    "sse_snapshot_state",
    "sse_stream",
]
