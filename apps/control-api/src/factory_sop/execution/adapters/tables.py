"""execution 拥有的 PostgreSQL 表。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from factory_sop.execution.model import StationGrant
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


__all__ = ["StationGrantRow"]
