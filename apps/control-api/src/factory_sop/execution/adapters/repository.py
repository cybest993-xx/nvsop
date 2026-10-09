"""execution 当前租约的 PostgreSQL 原子写入适配器。"""

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import Table, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from factory_sop.execution.adapters.tables import HandoverRow, StationGrantRow
from factory_sop.execution.model import HandoverConfirmation, StationGrant
from factory_sop.execution.repository import ExecutionGrantRepository, HandoverRepository


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
                    table.c.renewed_at < value.renewed_at,
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

    def for_holder(self, host_id: UUID) -> tuple[StationGrant, ...]:
        table = cast(Table, StationGrantRow.__table__)
        rows = (
            self._session.execute(
                select(*table.c)
                .where(table.c.holder_host_id == host_id)
                .order_by(table.c.station_id)
            )
            .mappings()
            .all()
        )
        return tuple(_from_mapping(row) for row in rows)

    def for_stations(self, station_ids: tuple[UUID, ...]) -> tuple[StationGrant, ...]:
        if not station_ids:
            return ()
        table = cast(Table, StationGrantRow.__table__)
        rows = (
            self._session.execute(
                select(*table.c)
                .where(table.c.station_id.in_(station_ids))
                .order_by(table.c.station_id)
            )
            .mappings()
            .all()
        )
        return tuple(_from_mapping(row) for row in rows)

    def for_station(self, station_id: UUID) -> StationGrant | None:
        table = cast(Table, StationGrantRow.__table__)
        row = (
            self._session.execute(
                select(*table.c).where(table.c.station_id == station_id).with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        return _from_mapping(row) if row is not None else None


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


class PostgresHandoverRepository(HandoverRepository):
    """用条件更新原子记录第二人确认，内容不符或已确认时不写入。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, value: HandoverConfirmation) -> None:
        self._session.add(
            HandoverRow(
                handover_id=value.handover_id,
                station_id=value.station_id,
                from_host_id=value.from_host_id,
                to_host_id=value.to_host_id,
                operator_id=value.operator_id,
                operator_confirmed_at=value.operator_confirmed_at,
                operator_risk_shown=value.operator_risk_shown,
                second_operator_id=value.second_operator_id,
                second_confirmed_at=value.second_confirmed_at,
                second_risk_shown=value.second_risk_shown,
            )
        )
        self._session.flush()

    def by_identifier(self, handover_id: UUID) -> HandoverConfirmation | None:
        row = self._session.get(HandoverRow, handover_id)
        return row.to_domain() if row is not None else None

    def confirm_second(self, value: HandoverConfirmation) -> HandoverConfirmation | None:
        table = cast(Table, HandoverRow.__table__)
        result = (
            self._session.execute(
                update(table)
                .where(
                    table.c.handover_id == value.handover_id,
                    table.c.station_id == value.station_id,
                    table.c.from_host_id == value.from_host_id,
                    table.c.to_host_id == value.to_host_id,
                    table.c.second_operator_id.is_(None),
                )
                .values(
                    second_operator_id=value.second_operator_id,
                    second_confirmed_at=value.second_confirmed_at,
                    second_risk_shown=value.second_risk_shown,
                )
                .returning(*table.c)
            )
            .mappings()
            .one_or_none()
        )
        return _handover_from_mapping(result) if result is not None else None


def _handover_from_mapping(value: RowMapping) -> HandoverConfirmation:
    return HandoverConfirmation(
        handover_id=cast(UUID, value["handover_id"]),
        station_id=cast(UUID, value["station_id"]),
        from_host_id=cast(UUID, value["from_host_id"]),
        to_host_id=cast(UUID, value["to_host_id"]),
        operator_id=cast(UUID, value["operator_id"]),
        operator_confirmed_at=cast(datetime, value["operator_confirmed_at"]),
        operator_risk_shown=cast(bool, value["operator_risk_shown"]),
        second_operator_id=cast(UUID | None, value["second_operator_id"]),
        second_confirmed_at=cast(datetime | None, value["second_confirmed_at"]),
        second_risk_shown=cast(bool | None, value["second_risk_shown"]),
    )


__all__ = ["PostgresExecutionGrantRepository", "PostgresHandoverRepository"]
