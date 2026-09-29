"""monitor 拥有的镜像值；它们是观测，不是判定权威。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from nvsop_contracts import ReportedDecision, ReportedHealth, ReportedSopInstance, ReportViolation


@dataclass(frozen=True, slots=True)
class MirroredDecision:
    report: ReportedDecision
    received_at: datetime
    stream_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class MirroredHealth:
    report: ReportedHealth
    received_at: datetime
    stream_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class MirroredSopInstance:
    report: ReportedSopInstance
    received_at: datetime


@dataclass(frozen=True, slots=True)
class MirroredViolation:
    """判定事件随附的一条已锁存违规；归档事实，不是第二份判定权威。

    `event_id` 由来源判定事件 id 与违规在该判定中的序号推导，因此同一判定的重复上报
    命中同一行；`decision_reported_at` 保存来源判定的上报时刻，与中心 `received_at`
    （接收时刻）分开。违规的真实发生时刻是其 evidence 锚点，随 violation 原样保留。
    """

    event_id: str
    decision_event_id: str
    host_id: str
    station_id: str
    instance_id: int
    report: ReportViolation
    decision_reported_at: str
    received_at: datetime

    def payload(self) -> dict[str, object]:
        """持久化与幂等比较使用的内容；接收时刻是列，不进 payload。"""
        return {
            "event_id": self.event_id,
            "decision_event_id": self.decision_event_id,
            "host_id": self.host_id,
            "station_id": self.station_id,
            "instance_id": self.instance_id,
            "reported_at": self.decision_reported_at,
            "violation": self.report.to_wire(),
        }

    def to_wire(self) -> dict[str, object]:
        """中心看板读取的投影：原事件身份、来源、原发生/接收时刻与违规内容。"""
        return {**self.payload(), "received_at": self.received_at.isoformat()}


__all__ = [
    "MirroredDecision",
    "MirroredHealth",
    "MirroredSopInstance",
    "MirroredViolation",
]
