"""execution 拥有的 PostgreSQL 表。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from factory_sop.execution.model import HandoverConfirmation, StationGrant
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
    station_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("device_station.id", ondelete="CASCADE"), index=True
    )
    from_host_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("device_inference_host.id", ondelete="CASCADE"), index=True
    )
    to_host_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("device_inference_host.id", ondelete="CASCADE")
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
