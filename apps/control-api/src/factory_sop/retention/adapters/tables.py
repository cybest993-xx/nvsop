"""retention 拥有的表：全局策略单行、工位整组覆盖记录与回收执行账本（§5.19）。

录像滚动窗口与原始素材窗口不在这里——前者按推理机配置（`device_inference_host`），后者归边缘媒体配置。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Integer, SmallInteger, String, Uuid
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from factory_sop.persistence import Table
from factory_sop.retention.model import (
    RetentionPolicy,
    RetentionPolicyMode,
    RetentionPolicyState,
)
from factory_sop.retention.sweep import RetentionSweepStatus
from factory_sop.retention.usecases import StationRetentionRecord


def _mode_enum(*, create_type: bool = True) -> SqlEnum:
    """数据库保存枚举值而不是成员名，保留 §5.15 规定的 snake_case 线格式。"""
    return SqlEnum(
        RetentionPolicyMode,
        name="retention_station_policy_mode",
        create_type=create_type,
        values_callable=lambda enum: [member.value for member in enum],
    )


def _sweep_status_enum() -> SqlEnum:
    return SqlEnum(
        RetentionSweepStatus,
        name="retention_sweep_status",
        create_type=False,
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


class RetentionSweepRow(Table):
    """一次回收任务的执行账本：范围、删除/跳过计数、原因与起止时刻（§5.19）。

    周期身份是主键：同一调度周期只有一个任务；``running`` 崩溃残留被下一次同周期任务覆盖，
    使失败与重启都可安全重跑，不积累孤儿账本行。
    """

    __tablename__ = "retention_sweep"

    cycle_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    status: Mapped[RetentionSweepStatus] = mapped_column(_sweep_status_enum())
    scope: Mapped[dict[str, object]] = mapped_column(JSONB())
    result: Mapped[dict[str, object] | None] = mapped_column(JSONB(), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
