"""monitor 边缘观测镜像的 SQLAlchemy 行模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from factory_sop.monitor.model import (
    MirroredDecision,
    MirroredHealth,
    MirroredObservation,
    MirroredSopInstance,
    MirroredViolation,
)
from factory_sop.persistence import Table
from nvsop_contracts import (
    ReportViolation,
    reported_decision_from_wire,
    reported_decision_to_wire,
    reported_health_from_wire,
    reported_health_to_wire,
    reported_observation_from_wire,
    reported_observation_to_wire,
    reported_sop_instance_from_wire,
    reported_sop_instance_to_wire,
)


class DecisionIdentityRow(Table):
    """decision 镜像的全局唯一身份：全局 ``event_id``/``stream_sequence`` 唯一在此普通表保留，
    事实表用含 ``stream_sequence`` 的复合外键指向它，使同一事件无法跨分区重复、序号不偏离身份。
    """

    __tablename__ = "monitor_decision_identity"
    # 事实表复合外键指向这个唯一约束；PK(event_id) 与 UNIQUE(stream_sequence) 分别保证事件
    # 身份与流序号全局唯一。
    __table_args__ = (UniqueConstraint("event_id", "received_at", "stream_sequence"),)

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stream_sequence: Mapped[int] = mapped_column(
        BigInteger(), Identity(), nullable=False, unique=True
    )


class ReportedDecisionRow(Table):
    __tablename__ = "monitor_reported_decision"
    __table_args__ = (
        ForeignKeyConstraint(
            ["event_id", "received_at", "stream_sequence"],
            [
                "monitor_decision_identity.event_id",
                "monitor_decision_identity.received_at",
                "monitor_decision_identity.stream_sequence",
            ],
            name="fk_monitor_reported_decision_identity",
            ondelete="CASCADE",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(255))
    host_id: Mapped[str] = mapped_column(String(128), index=True)
    station_id: Mapped[str] = mapped_column(String(128), index=True)
    backend_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, index=True
    )
    # 流内序号；全局唯一由 monitor_decision_identity 承载，此处保留副本供 SSE 游标查询。
    stream_sequence: Mapped[int] = mapped_column(BigInteger(), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    latched_at: Mapped[str | None] = mapped_column(Text(), nullable=True)
    realtime: Mapped[bool] = mapped_column(default=False, nullable=False)

    def to_domain(self) -> MirroredDecision:
        return MirroredDecision(
            report=reported_decision_from_wire(cast(dict[str, object], self.payload)),
            received_at=self.received_at,
            stream_sequence=self.stream_sequence,
            latched_at=self.latched_at,
            realtime=self.realtime,
        )

    @classmethod
    def from_domain(cls, value: MirroredDecision) -> ReportedDecisionRow:
        report = value.report
        row = cls(
            event_id=report.event_id,
            trace_id=report.trace_id,
            host_id=report.host_id,
            station_id=report.station_id,
            backend_id=report.backend_id,
            received_at=value.received_at,
            payload=reported_decision_to_wire(report),
            latched_at=value.latched_at,
            realtime=value.realtime,
        )
        if value.stream_sequence is not None:
            row.stream_sequence = value.stream_sequence
        return row


class HealthIdentityRow(Table):
    """health 镜像的全局唯一身份，理由同 decision。"""

    __tablename__ = "monitor_health_identity"
    __table_args__ = (UniqueConstraint("event_id", "received_at", "stream_sequence"),)

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stream_sequence: Mapped[int] = mapped_column(
        BigInteger(), Identity(), nullable=False, unique=True
    )


class ReportedHealthRow(Table):
    __tablename__ = "monitor_reported_health"
    __table_args__ = (
        ForeignKeyConstraint(
            ["event_id", "received_at", "stream_sequence"],
            [
                "monitor_health_identity.event_id",
                "monitor_health_identity.received_at",
                "monitor_health_identity.stream_sequence",
            ],
            name="fk_monitor_reported_health_identity",
            ondelete="CASCADE",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(255))
    host_id: Mapped[str] = mapped_column(String(128), index=True)
    station_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    # 每路流身份；判定有效性与时间锚按流隔离，查询与看板按此定位一路流。
    stream_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, index=True
    )
    # 流内序号；全局唯一由 monitor_health_identity 承载，此处保留副本供 SSE 游标查询。
    stream_sequence: Mapped[int] = mapped_column(BigInteger(), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)

    def to_domain(self) -> MirroredHealth:
        return MirroredHealth(
            report=reported_health_from_wire(cast(dict[str, object], self.payload)),
            received_at=self.received_at,
            stream_sequence=self.stream_sequence,
        )

    @classmethod
    def from_domain(cls, value: MirroredHealth) -> ReportedHealthRow:
        report = value.report
        row = cls(
            event_id=report.event_id,
            trace_id=report.trace_id,
            host_id=report.host_id,
            station_id=report.station_id,
            stream_id=report.stream_id,
            received_at=value.received_at,
            payload=reported_health_to_wire(report),
        )
        if value.stream_sequence is not None:
            row.stream_sequence = value.stream_sequence
        return row


class ReportedSopInstanceRow(Table):
    __tablename__ = "monitor_sop_instance"
    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    host_id: Mapped[str] = mapped_column(String(128), index=True)
    station_id: Mapped[str] = mapped_column(String(128), index=True)
    instance_id: Mapped[int] = mapped_column(BigInteger())
    opened_at: Mapped[float]
    closed_at: Mapped[float | None] = mapped_column(nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    open_boundary_signal: Mapped[str | None] = mapped_column(Text(), nullable=True)
    close_boundary_signal: Mapped[str | None] = mapped_column(Text(), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)

    def to_domain(self) -> MirroredSopInstance:
        return MirroredSopInstance(
            report=reported_sop_instance_from_wire(cast(dict[str, object], self.payload)),
            received_at=self.received_at,
        )

    @classmethod
    def from_domain(cls, value: MirroredSopInstance) -> ReportedSopInstanceRow:
        report = value.report
        return cls(
            event_id=report.event_id,
            host_id=report.host_id,
            station_id=report.station_id,
            instance_id=report.instance_id,
            opened_at=report.opened_at,
            closed_at=report.closed_at,
            close_reason=report.close_reason,
            open_boundary_signal=report.open_boundary_signal,
            close_boundary_signal=report.close_boundary_signal,
            received_at=value.received_at,
            payload=reported_sop_instance_to_wire(report),
        )


class ObservationIdentityRow(Table):
    """observation 镜像的全局唯一身份；观测不参与 SSE，故无流序号。"""

    __tablename__ = "monitor_observation_identity"
    __table_args__ = (UniqueConstraint("event_id", "received_at"),)

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReportedObservationRow(Table):
    """产生时冻结的归一化观测镜像; 按事件 id 幂等, 不参与 SSE 投影。"""

    __tablename__ = "monitor_observation"
    __table_args__ = (
        ForeignKeyConstraint(
            ["event_id", "received_at"],
            ["monitor_observation_identity.event_id", "monitor_observation_identity.received_at"],
            name="fk_monitor_observation_identity",
            ondelete="CASCADE",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(255))
    host_id: Mapped[str] = mapped_column(String(128), index=True)
    station_id: Mapped[str] = mapped_column(String(128), index=True)
    instance_id: Mapped[int] = mapped_column(BigInteger(), index=True)
    source: Mapped[str] = mapped_column(String(64))
    signal: Mapped[str] = mapped_column(Text())
    observed_at: Mapped[float]
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)

    def to_domain(self) -> MirroredObservation:
        return MirroredObservation(
            report=reported_observation_from_wire(cast(dict[str, object], self.payload)),
            received_at=self.received_at,
        )

    @classmethod
    def from_domain(cls, value: MirroredObservation) -> ReportedObservationRow:
        report = value.report
        return cls(
            event_id=report.event_id,
            trace_id=report.trace_id,
            host_id=report.host_id,
            station_id=report.station_id,
            instance_id=report.instance_id,
            source=report.source,
            signal=report.signal,
            observed_at=report.observed_at,
            received_at=value.received_at,
            payload=reported_observation_to_wire(report),
        )


class ReportedDisposalRow(Table):
    __tablename__ = "monitor_disposal"

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    host_id: Mapped[str] = mapped_column(String(128), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ReportedViolationRow(Table):
    __tablename__ = "monitor_violation"

    event_id: Mapped[str] = mapped_column(Text(), primary_key=True)
    decision_event_id: Mapped[str] = mapped_column(String(255), index=True)
    host_id: Mapped[str] = mapped_column(String(128), index=True)
    station_id: Mapped[str] = mapped_column(String(128), index=True)
    instance_id: Mapped[int] = mapped_column(BigInteger(), index=True)
    # 原因码在契约边界是开放字符串 (ADR-0003), 不能收窄成定长列。
    reason_code: Mapped[str] = mapped_column(Text())
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)

    def to_domain(self) -> MirroredViolation:
        payload = cast(dict[str, object], self.payload)
        return MirroredViolation(
            event_id=self.event_id,
            decision_event_id=self.decision_event_id,
            host_id=self.host_id,
            station_id=self.station_id,
            instance_id=self.instance_id,
            report=ReportViolation.from_wire(cast(dict[str, object], payload["violation"])),
            decision_reported_at=cast(str, payload["reported_at"]),
            received_at=self.received_at,
            latched_at=cast(str | None, payload.get("latched_at")),
        )

    @classmethod
    def from_domain(cls, value: MirroredViolation) -> ReportedViolationRow:
        return cls(
            event_id=value.event_id,
            decision_event_id=value.decision_event_id,
            host_id=value.host_id,
            station_id=value.station_id,
            instance_id=value.instance_id,
            reason_code=value.report.reason_code,
            received_at=value.received_at,
            payload=value.payload(),
        )
