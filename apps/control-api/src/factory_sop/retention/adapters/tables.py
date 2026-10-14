"""retention 拥有的表：全局策略单行与工位整组覆盖记录（§5.19）。

录像滚动窗口与原始素材窗口不在这里——前者按推理机配置（`device_inference_host`），后者归边缘媒体配置。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Enum, Integer, SmallInteger, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from factory_sop.persistence import Table
from factory_sop.retention.model import (
    RetentionPolicy,
    RetentionPolicyMode,
    RetentionPolicyState,
)
from factory_sop.retention.usecases import StationRetentionRecord


def _mode_enum(*, create_type: bool = True) -> Enum:
    """数据库保存枚举值而不是成员名，保留 §5.15 规定的 snake_case 线格式。"""
    return Enum(
        RetentionPolicyMode,
        name="retention_station_policy_mode",
        create_type=create_type,
        values_callable=lambda enum: [member.value for member in enum],
    )


class RetentionPolicyRow(Table):
    __tablename__ = "retention_policy"

    singleton: Mapped[int] = mapped_column(SmallInteger(), primary_key=True)
    policy: Mapped[dict[str, object]] = mapped_column(JSONB())
    revision: Mapped[int] = mapped_column(Integer())

    def to_domain(self) -> RetentionPolicyState:
        return RetentionPolicyState(
            policy=RetentionPolicy.from_wire(self.policy), revision=self.revision
        )

    @classmethod
    def from_domain(cls, value: RetentionPolicyState) -> RetentionPolicyRow:
        return cls(singleton=1, policy=value.policy.to_wire(), revision=value.revision)


class StationRetentionPolicyRow(Table):
    """工位的整组保留覆盖记录；没有本行即整组跟随全局默认。"""

    __tablename__ = "retention_station_policy"

    station_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    mode: Mapped[RetentionPolicyMode] = mapped_column(_mode_enum())
    overrides: Mapped[dict[str, object] | None] = mapped_column(JSONB(), nullable=True)
    revision: Mapped[int] = mapped_column(Integer())

    def to_domain(self) -> StationRetentionRecord:
        return StationRetentionRecord(
            station_id=self.station_id,
            mode=self.mode,
            overrides=(
                None if self.overrides is None else RetentionPolicy.from_wire(self.overrides)
            ),
            revision=self.revision,
        )

    @classmethod
    def from_domain(cls, value: StationRetentionRecord) -> StationRetentionPolicyRow:
        return cls(
            station_id=value.station_id,
            mode=value.mode,
            overrides=None if value.overrides is None else value.overrides.to_wire(),
            revision=value.revision,
        )


__all__ = ["RetentionPolicyRow", "StationRetentionPolicyRow"]
