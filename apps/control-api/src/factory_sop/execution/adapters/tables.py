"""execution 拥有的 PostgreSQL 表。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from factory_sop.execution.model import (
    HandoverConfirmation,
    NormalHandover,
    NormalHandoverStatus,
    StationGrant,
)
from factory_sop.persistence import Table


class StationGrantRow(Table):
    """每个工位恰有零或一条中心当前租约。"""

    __tablename__ = "execution_station_grant"
    __table_args__ = (
        CheckConstraint(
            "lease_expires_at = renewed_at + INTERVAL '7 days'",
            name="ck_execution_station_grant_seven_day_ttl",
        ),
        UniqueConstraint("grant_id", name="uq_execution_station_grant_grant_id"),
    )

    station_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("device_station.id", ondelete="CASCADE"), primary_key=True
    )
    grant_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    holder_host_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("device_inference_host.id", ondelete="CASCADE"), index=True
    )
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    renewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)

    def to_domain(self) -> StationGrant:
        return StationGrant(
            grant_id=self.grant_id,
            station_id=self.station_id,
            holder_host_id=self.holder_host_id,
            lease_expires_at=self.lease_expires_at,
            renewed_at=self.renewed_at,
            request_id=self.request_id,
        )


class NormalHandoverRow(Table):
    """正常交权请求；旧租约身份与命令身份建立后不可变。"""

    __tablename__ = "execution_normal_handover"

    handover_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    station_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("device_station.id", ondelete="RESTRICT"), index=True
    )
    from_host_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("device_inference_host.id", ondelete="RESTRICT"), index=True
    )
    to_host_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("device_inference_host.id", ondelete="RESTRICT")
    )
    grant_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    command_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False, unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    requested_by: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timed_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    failure_detail: Mapped[str | None] = mapped_column(String(255))
    new_grant_id: Mapped[UUID | None] = mapped_column(Uuid())
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    granted_by: Mapped[UUID | None] = mapped_column(Uuid())

    def to_domain(self) -> NormalHandover:
        return NormalHandover(
            handover_id=self.handover_id,
            station_id=self.station_id,
            from_host_id=self.from_host_id,
            to_host_id=self.to_host_id,
            grant_id=self.grant_id,
            command_id=self.command_id,
            idempotency_key=self.idempotency_key,
            requested_by=self.requested_by,
            requested_at=self.requested_at,
            expires_at=self.expires_at,
            status=NormalHandoverStatus(self.status),
            confirmed_at=self.confirmed_at,
            cancelled_at=self.cancelled_at,
            timed_out_at=self.timed_out_at,
            failed_at=self.failed_at,
            failure_code=self.failure_code,
            failure_detail=self.failure_detail,
            new_grant_id=self.new_grant_id,
            granted_at=self.granted_at,
            granted_by=self.granted_by,
        )

    @classmethod
    def from_domain(cls, value: NormalHandover) -> NormalHandoverRow:
        return cls(
            handover_id=value.handover_id,
            station_id=value.station_id,
            from_host_id=value.from_host_id,
            to_host_id=value.to_host_id,
            grant_id=value.grant_id,
            command_id=value.command_id,
            idempotency_key=value.idempotency_key,
            requested_by=value.requested_by,
            requested_at=value.requested_at,
            expires_at=value.expires_at,
            status=value.status.value,
            confirmed_at=value.confirmed_at,
            cancelled_at=value.cancelled_at,
            timed_out_at=value.timed_out_at,
            failed_at=value.failed_at,
            failure_code=value.failure_code,
            failure_detail=value.failure_detail,
            new_grant_id=value.new_grant_id,
            granted_at=value.granted_at,
            granted_by=value.granted_by,
        )


class HandoverRow(Table):
    """一次强制改绑请求及其两人确认事实。内容建立后不可变。

    工位/旧机/目标机是合法外键；两名确认人按仓库约定存为合法 UUID（跨模块不设 auth 外键，
    与 `created_by` 等归属列一致）。`second_*` 三列同生同灭，用 check 约束固定。
    """

    __tablename__ = "execution_handover"
    __table_args__ = (
        CheckConstraint(
            "(second_operator_id IS NULL) = (second_confirmed_at IS NULL) "
            "AND (second_operator_id IS NULL) = (second_risk_shown IS NULL)",
            name="second_confirmation_together",
        ),
    )

    handover_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    # 三人/三方引用一律 RESTRICT：删除仍被强制改绑历史引用的设备必须失败，
    # 不能把两人确认过的诊断事实随一次设备 CRUD 静默级联清除（0050，§5.17）。
    station_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("device_station.id", ondelete="RESTRICT"), index=True
    )
    from_host_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("device_inference_host.id", ondelete="RESTRICT"), index=True
    )
    to_host_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("device_inference_host.id", ondelete="RESTRICT")
    )
    operator_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    operator_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    operator_risk_shown: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    second_operator_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    second_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    second_risk_shown: Mapped[bool | None] = mapped_column(Boolean(), nullable=True)

    def to_domain(self) -> HandoverConfirmation:
        return HandoverConfirmation(
            handover_id=self.handover_id,
            station_id=self.station_id,
            from_host_id=self.from_host_id,
            to_host_id=self.to_host_id,
            operator_id=self.operator_id,
            operator_confirmed_at=self.operator_confirmed_at,
            operator_risk_shown=self.operator_risk_shown,
            second_operator_id=self.second_operator_id,
            second_confirmed_at=self.second_confirmed_at,
            second_risk_shown=self.second_risk_shown,
        )


__all__ = ["HandoverRow", "StationGrantRow"]
