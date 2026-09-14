"""monitor 边缘观测镜像的 SQLAlchemy 行模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import BigInteger, DateTime, Identity, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from factory_sop.monitor.model import MirroredDecision, MirroredHealth
from factory_sop.persistence import Table
from nvsop_contracts import (
    reported_decision_from_wire,
    reported_decision_to_wire,
    reported_health_from_wire,
    reported_health_to_wire,
)


class ReportedDecisionRow(Table):
    __tablename__ = "monitor_reported_decision"

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(255))
    host_id: Mapped[str] = mapped_column(String(128), index=True)
    station_id: Mapped[str] = mapped_column(String(128), index=True)
    backend_id: Mapped[str] = mapped_column(String(128), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    stream_sequence: Mapped[int] = mapped_column(
        BigInteger(), Identity(), nullable=False, unique=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)

    def to_domain(self) -> MirroredDecision:
        return MirroredDecision(
            report=reported_decision_from_wire(cast(dict[str, object], self.payload)),
            received_at=self.received_at,
            stream_sequence=self.stream_sequence,
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
        )
        if value.stream_sequence is not None:
            row.stream_sequence = value.stream_sequence
        return row


class ReportedHealthRow(Table):
    __tablename__ = "monitor_reported_health"

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(255))
    host_id: Mapped[str] = mapped_column(String(128), index=True)
    station_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    stream_sequence: Mapped[int] = mapped_column(
        BigInteger(), Identity(), nullable=False, unique=True
    )
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
            received_at=value.received_at,
            payload=reported_health_to_wire(report),
        )
        if value.stream_sequence is not None:
            row.stream_sequence = value.stream_sequence
        return row
