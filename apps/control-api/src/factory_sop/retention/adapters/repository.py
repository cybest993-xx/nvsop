"""retention 自己的 PostgreSQL 适配器与配置投影；方法都不提交事务。"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, Table, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.orm import Session

from factory_sop.retention.adapters.tables import (
    RetentionPolicyRow,
    StationRetentionPolicyRow,
)
from factory_sop.retention.model import (
    RetentionPolicyMode,
    RetentionPolicyState,
    resolve_retention_policy,
)
from factory_sop.retention.usecases import (
    RetentionPolicyRepository,
    StationRetentionOverrideRepository,
    StationRetentionProjection,
    StationRetentionProjectionGateway,
    StationRetentionRecord,
    default_retention_policy,
)
from nvsop_contracts import ResolvedRetentionPolicy


class PostgresRetentionPolicyRepository(RetentionPolicyRepository):
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_global(self) -> RetentionPolicyState | None:
        row = self._session.scalar(
            select(RetentionPolicyRow)
            .where(RetentionPolicyRow.singleton == 1)
            .execution_options(populate_existing=True)
        )
        return None if row is None else row.to_domain()

    def lock_current(self) -> RetentionPolicyState | None:
        """共享行锁读取当前策略；未持久化时为 ``None``，且没有可锁的行。"""
        row = self._session.scalar(
            select(RetentionPolicyRow)
            .where(RetentionPolicyRow.singleton == 1)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        return None if row is None else row.to_domain()

    def replace_if_current(self, *, expected_revision: int, value: RetentionPolicyState) -> bool:
        """expected_revision==0 时仅首次插入；否则只在 revision 仍相等时更新。"""
        table = cast(Table, RetentionPolicyRow.__table__)
        policy, revision = value.policy.to_wire(), value.revision
        if expected_revision == 0:
            insert_statement = (
                postgres_insert(table)
                .values(singleton=1, policy=policy, revision=revision)
                .on_conflict_do_nothing(index_elements=[table.c.singleton])
                .returning(table.c.singleton)
            )
            return self._session.execute(insert_statement).scalar_one_or_none() is not None
        update_statement = (
            update(table)
            .where(table.c.singleton == 1, table.c.revision == expected_revision)
            .values(policy=policy, revision=revision)
        )
        result = cast("CursorResult[Any]", self._session.execute(update_statement))
        return result.rowcount == 1


class PostgresStationRetentionOverrideRepository(StationRetentionOverrideRepository):
    """`retention_station_policy` 的版本条件写；无行即该工位整组跟随全局默认。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, station_id: UUID) -> StationRetentionRecord | None:
        row = self._session.scalar(
            select(StationRetentionPolicyRow)
            .where(StationRetentionPolicyRow.station_id == station_id)
            .execution_options(populate_existing=True)
        )
        return None if row is None else row.to_domain()

    def list_all(self) -> tuple[StationRetentionRecord, ...]:
        """返回全部覆盖记录；全局变更据此排除带自定义整组的工位。"""
        rows = self._session.scalars(
            select(StationRetentionPolicyRow).order_by(StationRetentionPolicyRow.station_id)
        ).all()
        return tuple(row.to_domain() for row in rows)

    def insert_if_absent(self, record: StationRetentionRecord) -> bool:
        """expected_revision==0 的首次建立：唯一键冲突即表示并发下另一个请求已经建过。"""
        table = cast(Table, StationRetentionPolicyRow.__table__)
        insert_statement = (
            postgres_insert(table)
            .values(
                station_id=record.station_id,
                mode=record.mode,
                overrides=None if record.overrides is None else record.overrides.to_wire(),
                revision=record.revision,
            )
            .on_conflict_do_nothing(index_elements=[table.c.station_id])
            .returning(table.c.station_id)
        )
        return self._session.execute(insert_statement).scalar_one_or_none() is not None

    def lock_current(self, station_id: UUID) -> StationRetentionRecord | None:
        """共享行锁读取该工位覆盖记录；没有记录时为 ``None``。"""
        row = self._session.scalar(
            select(StationRetentionPolicyRow)
            .where(StationRetentionPolicyRow.station_id == station_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        return None if row is None else row.to_domain()

    def replace_if_current(self, *, expected_revision: int, record: StationRetentionRecord) -> bool:
        table = cast(Table, StationRetentionPolicyRow.__table__)
        update_statement = (
            update(table)
            .where(
                table.c.station_id == record.station_id,
                table.c.revision == expected_revision,
            )
            .values(
                mode=record.mode,
                overrides=None if record.overrides is None else record.overrides.to_wire(),
                revision=record.revision,
            )
        )
        result = cast("CursorResult[Any]", self._session.execute(update_statement))
        return result.rowcount == 1


class RepositoryStationRetentionProjectionGateway(StationRetentionProjectionGateway):
    """从 retention 自己的两张表构造配置组合所需的生效组投影；不读任何 device 表。"""

    def __init__(
        self,
        policies: RetentionPolicyRepository,
        overrides: StationRetentionOverrideRepository,
    ) -> None:
        self._policies = policies
        self._overrides = overrides

    def for_station(self, station_id: UUID) -> StationRetentionProjection:
        record = self._overrides.get(station_id)
        default = default_retention_policy(self._policies)
        mode = RetentionPolicyMode.FOLLOW_GLOBAL if record is None else record.mode
        overrides = None if record is None else record.overrides
        effective = resolve_retention_policy(default, mode=mode, overrides=overrides)
        return StationRetentionProjection(
            station_id=station_id,
            revision=0 if record is None else record.revision,
            effective=ResolvedRetentionPolicy.from_wire(effective.to_wire()),
        )


__all__ = [
    "PostgresRetentionPolicyRepository",
    "PostgresStationRetentionOverrideRepository",
    "RepositoryStationRetentionProjectionGateway",
]
