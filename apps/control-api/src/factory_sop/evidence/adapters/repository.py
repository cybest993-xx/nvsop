"""evidence 中心引用登记的 PostgreSQL 适配器。"""

from __future__ import annotations

from typing import cast

from sqlalchemy import Table, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.orm import Session

from factory_sop.evidence.adapters.tables import EvidenceReferenceRow
from factory_sop.evidence.model import EvidenceReference, EvidenceStatus
from factory_sop.evidence.repository import EvidenceRepository


class PostgresEvidenceRepository(EvidenceRepository):
    def __init__(self, session: Session) -> None:
        self._session = session

    def find(self, evidence_id: str) -> EvidenceReference | None:
        row = self._session.get(EvidenceReferenceRow, evidence_id)
        return None if row is None else row.to_domain()

    def insert(self, value: EvidenceReference) -> bool:
        row = EvidenceReferenceRow.from_domain(value)
        table = cast(Table, EvidenceReferenceRow.__table__)
        statement = (
            postgres_insert(table)
            .values(**_columns(row))
            .on_conflict_do_nothing(index_elements=[table.c.evidence_id])
            .returning(table.c.evidence_id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def replace(self, value: EvidenceReference) -> None:
        row = EvidenceReferenceRow.from_domain(value)
        table = cast(Table, EvidenceReferenceRow.__table__)
        statement = (
            update(table).where(table.c.evidence_id == row.evidence_id).values(**_columns(row))
        )
        self._session.execute(statement)

    def page(
        self,
        *,
        page: int,
        page_size: int,
        status: EvidenceStatus | None = None,
        station_id: str | None = None,
        instance_id: int | None = None,
    ) -> tuple[tuple[EvidenceReference, ...], int]:
        statement = select(EvidenceReferenceRow)
        if status is not None:
            statement = statement.where(EvidenceReferenceRow.status == status.value)
        if station_id is not None:
            statement = statement.where(EvidenceReferenceRow.station_id == station_id)
        if instance_id is not None:
            statement = statement.where(EvidenceReferenceRow.instance_id == instance_id)
        rows = self._session.scalars(
            statement.order_by(
                EvidenceReferenceRow.received_at.desc(),
                EvidenceReferenceRow.evidence_id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        total = self._session.scalar(select(func.count()).select_from(statement.subquery()))
        return tuple(row.to_domain() for row in rows), int(total or 0)


def _columns(row: EvidenceReferenceRow) -> dict[str, object]:
    return {
        column.name: getattr(row, column.key) for column in EvidenceReferenceRow.__table__.columns
    }


__all__ = ["PostgresEvidenceRepository"]
