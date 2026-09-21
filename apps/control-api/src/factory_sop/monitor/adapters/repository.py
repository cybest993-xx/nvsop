"""monitor 幂等观测镜像的 PostgreSQL 适配器。"""

from __future__ import annotations

from typing import cast

from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.orm import Session

from factory_sop.monitor.adapters.tables import (
    ReportedDecisionRow,
    ReportedHealthRow,
    ReportedSopInstanceRow,
)
from factory_sop.monitor.errors import MonitorRefusedError
from factory_sop.monitor.model import MirroredDecision, MirroredHealth, MirroredSopInstance
from factory_sop.monitor.repository import MonitorRepository


class PostgresMonitorRepository(MonitorRepository):
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_decision(self, value: MirroredDecision) -> bool:
        row = ReportedDecisionRow.from_domain(value)
        table = cast(Table, ReportedDecisionRow.__table__)
        statement = postgres_insert(table).values(
            event_id=row.event_id,
            trace_id=row.trace_id,
            host_id=row.host_id,
            station_id=row.station_id,
            backend_id=row.backend_id,
            received_at=row.received_at,
            payload=row.payload,
        )
        result = self._session.execute(
            statement.on_conflict_do_nothing(index_elements=[table.c.event_id]).returning(
                table.c.event_id
            )
        )
        if result.scalar_one_or_none() is not None:
            return True
        existing = self._session.get(ReportedDecisionRow, row.event_id)
        if existing is None:
            raise RuntimeError("decision mirror insert conflicted without a visible row")
        _ensure_same(existing.payload, row.payload, "decision", row.event_id)
        return False

    def upsert_health(self, value: MirroredHealth) -> bool:
        row = ReportedHealthRow.from_domain(value)
        table = cast(Table, ReportedHealthRow.__table__)
        statement = postgres_insert(table).values(
            event_id=row.event_id,
            trace_id=row.trace_id,
            host_id=row.host_id,
            station_id=row.station_id,
            received_at=row.received_at,
            payload=row.payload,
        )
        result = self._session.execute(
            statement.on_conflict_do_nothing(index_elements=[table.c.event_id]).returning(
                table.c.event_id
            )
        )
        if result.scalar_one_or_none() is not None:
            return True
        existing = self._session.get(ReportedHealthRow, row.event_id)
        if existing is None:
            raise RuntimeError("health mirror insert conflicted without a visible row")
        _ensure_same(existing.payload, row.payload, "health", row.event_id)
        return False

    def upsert_instance(self, value: MirroredSopInstance) -> bool:
        row = ReportedSopInstanceRow.from_domain(value)
        table = cast(Table, ReportedSopInstanceRow.__table__)
        statement = postgres_insert(table).values(
            event_id=row.event_id,
            host_id=row.host_id,
            station_id=row.station_id,
            instance_id=row.instance_id,
            opened_at=row.opened_at,
            closed_at=row.closed_at,
            close_reason=row.close_reason,
            received_at=row.received_at,
            payload=row.payload,
        )
        result = self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[table.c.event_id],
                set_={
                    "closed_at": statement.excluded.closed_at,
                    "close_reason": statement.excluded.close_reason,
                    "received_at": statement.excluded.received_at,
                    "payload": statement.excluded.payload,
                },
                where=table.c.closed_at.is_(None) & statement.excluded.closed_at.is_not(None),
            ).returning(table.c.event_id)
        )
        if result.scalar_one_or_none() is not None:
            return True
        existing = self._session.get(ReportedSopInstanceRow, row.event_id)
        if existing is None:
            raise RuntimeError("instance mirror upsert completed without a visible row")
        if existing.closed_at is not None and row.closed_at is None:
            return False
        _ensure_same(existing.payload, row.payload, "instance", row.event_id)
        return False

    def recent_instances(self, *, limit: int) -> tuple[MirroredSopInstance, ...]:
        rows = self._session.scalars(
            select(ReportedSopInstanceRow)
            .order_by(ReportedSopInstanceRow.received_at.desc())
            .limit(limit)
        ).all()
        return tuple(row.to_domain() for row in rows)

    def recent_decisions(self, *, limit: int) -> tuple[MirroredDecision, ...]:
        rows = self._session.scalars(
            select(ReportedDecisionRow)
            .order_by(ReportedDecisionRow.stream_sequence.desc())
            .limit(limit)
        ).all()
        return tuple(row.to_domain() for row in rows)

    def recent_health(self, *, limit: int) -> tuple[MirroredHealth, ...]:
        rows = self._session.scalars(
            select(ReportedHealthRow)
            .order_by(ReportedHealthRow.stream_sequence.desc())
            .limit(limit)
        ).all()
        return tuple(row.to_domain() for row in rows)

    def decisions_after_sequence(
        self, *, after_sequence: int, limit: int
    ) -> tuple[MirroredDecision, ...]:
        rows = self._session.scalars(
            select(ReportedDecisionRow)
            .where(ReportedDecisionRow.stream_sequence > after_sequence)
            .order_by(ReportedDecisionRow.stream_sequence)
            .limit(limit)
        ).all()
        return tuple(row.to_domain() for row in rows)

    def health_after_sequence(
        self, *, after_sequence: int, limit: int
    ) -> tuple[MirroredHealth, ...]:
        rows = self._session.scalars(
            select(ReportedHealthRow)
            .where(ReportedHealthRow.stream_sequence > after_sequence)
            .order_by(ReportedHealthRow.stream_sequence)
            .limit(limit)
        ).all()
        return tuple(row.to_domain() for row in rows)

    def decision_sequence_for_event(self, event_id: str) -> int | None:
        return self._session.scalar(
            select(ReportedDecisionRow.stream_sequence).where(
                ReportedDecisionRow.event_id == event_id
            )
        )

    def health_sequence_for_event(self, event_id: str) -> int | None:
        return self._session.scalar(
            select(ReportedHealthRow.stream_sequence).where(ReportedHealthRow.event_id == event_id)
        )


def _ensure_same(
    existing: dict[str, object], incoming: dict[str, object], kind: str, event_id: str
) -> None:
    if existing != incoming:
        raise MonitorRefusedError(
            f"{kind} event id {event_id!r} was reused with a different payload"
        )


__all__ = ["PostgresMonitorRepository"]
