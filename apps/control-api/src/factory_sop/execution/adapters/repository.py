"""execution 当前租约的 PostgreSQL 原子写入适配器。"""

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import Table, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from factory_sop.execution.adapters.tables import StationGrantRow
from factory_sop.execution.model import StationGrant
from factory_sop.execution.repository import ExecutionGrantRepository


class PostgresExecutionGrantRepository(ExecutionGrantRepository):
    """用单行冲突和条件写入强制工位租约排他。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def acquire_if_available(self, value: StationGrant) -> StationGrant | None:
        table = cast(Table, StationGrantRow.__table__)
        statement = postgres_insert(table).values(**_values(value))
        result = (
            self._session.execute(
                statement.on_conflict_do_update(
                    index_elements=[table.c.station_id],
                    set_={
                        "grant_id": value.grant_id,
                        "holder_host_id": value.holder_host_id,
                        "lease_expires_at": value.lease_expires_at,
                        "renewed_at": value.renewed_at,
                        "request_id": value.request_id,
                    },
                    where=table.c.lease_expires_at <= value.renewed_at,
                ).returning(*table.c)
            )
            .mappings()
            .one_or_none()
        )
        return _from_mapping(result) if result is not None else None

    def renew_if_current(self, value: StationGrant) -> StationGrant | None:
        table = cast(Table, StationGrantRow.__table__)
        result = (
            self._session.execute(
                update(table)
                .where(
                    table.c.station_id == value.station_id,
                    table.c.grant_id == value.grant_id,
                    table.c.holder_host_id == value.holder_host_id,
                    table.c.lease_expires_at > value.renewed_at,
                )
                .values(
                    lease_expires_at=value.lease_expires_at,
                    renewed_at=value.renewed_at,
                    request_id=value.request_id,
                )
                .returning(*table.c)
            )
            .mappings()
            .one_or_none()
        )
        return _from_mapping(result) if result is not None else None


def _values(value: StationGrant) -> dict[str, object]:
    return {
        "grant_id": value.grant_id,
        "station_id": value.station_id,
        "holder_host_id": value.holder_host_id,
        "lease_expires_at": value.lease_expires_at,
        "renewed_at": value.renewed_at,
        "request_id": value.request_id,
    }


def _from_mapping(value: RowMapping) -> StationGrant:
    return StationGrant(
        grant_id=cast(UUID, value["grant_id"]),
        station_id=cast(UUID, value["station_id"]),
        holder_host_id=cast(UUID, value["holder_host_id"]),
        lease_expires_at=cast(datetime, value["lease_expires_at"]),
        renewed_at=cast(datetime, value["renewed_at"]),
        request_id=cast(UUID, value["request_id"]),
    )


__all__ = ["PostgresExecutionGrantRepository"]
