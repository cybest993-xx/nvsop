"""monitor 幂等观测镜像的 PostgreSQL 适配器。"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import Table, and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.orm import Session

from factory_sop.monitor.adapters.tables import ReportedDecisionRow, ReportedHealthRow
from factory_sop.monitor.errors import MonitorRefusedError
from factory_sop.monitor.model import MirroredDecision, MirroredHealth
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

    def recent_decisions(
        self, *, limit: int, through_sequence: int | None = None
    ) -> tuple[MirroredDecision, ...]:
        statement = select(ReportedDecisionRow)
        if through_sequence is not None:
            statement = statement.where(ReportedDecisionRow.stream_sequence <= through_sequence)
        rows = self._session.scalars(
            statement.order_by(ReportedDecisionRow.stream_sequence.desc()).limit(limit)
        ).all()
        return tuple(row.to_domain() for row in rows)

    def recent_health(
        self, *, limit: int, through_sequence: int | None = None
    ) -> tuple[MirroredHealth, ...]:
        statement = select(ReportedHealthRow)
        if through_sequence is not None:
            statement = statement.where(ReportedHealthRow.stream_sequence <= through_sequence)
        rows = self._session.scalars(
            statement.order_by(ReportedHealthRow.stream_sequence.desc()).limit(limit)
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

    def event_cursor_for(self, event_id: str) -> tuple[datetime, str] | None:
        decision_at = self._session.scalar(
            select(ReportedDecisionRow.received_at).where(ReportedDecisionRow.event_id == event_id)
        )
        health_at = self._session.scalar(
            select(ReportedHealthRow.received_at).where(ReportedHealthRow.event_id == event_id)
        )
        received_at_values = tuple(value for value in (decision_at, health_at) if value is not None)
        if not received_at_values:
            return None
        return min(received_at_values), event_id

    def decisions_after_cursor(
        self,
        *,
        after: tuple[datetime, str],
        limit: int | None,
        through_sequence: int | None = None,
    ) -> tuple[MirroredDecision, ...]:
        received_at, event_id = after
        after_cursor = or_(
            ReportedDecisionRow.received_at > received_at,
            and_(
                ReportedDecisionRow.received_at == received_at,
                ReportedDecisionRow.event_id > event_id,
            ),
        )
        statement = select(ReportedDecisionRow).where(after_cursor)
        if through_sequence is not None:
            statement = statement.where(ReportedDecisionRow.stream_sequence <= through_sequence)
        statement = statement.order_by(
            ReportedDecisionRow.received_at, ReportedDecisionRow.event_id
        )
        if limit is not None:
            statement = statement.limit(limit)
        rows = self._session.scalars(statement).all()
        return tuple(row.to_domain() for row in rows)

    def health_after_cursor(
        self,
        *,
        after: tuple[datetime, str],
        limit: int | None,
        through_sequence: int | None = None,
    ) -> tuple[MirroredHealth, ...]:
        received_at, event_id = after
        after_cursor = or_(
            ReportedHealthRow.received_at > received_at,
            and_(
                ReportedHealthRow.received_at == received_at,
                ReportedHealthRow.event_id > event_id,
            ),
        )
        statement = select(ReportedHealthRow).where(after_cursor)
        if through_sequence is not None:
            statement = statement.where(ReportedHealthRow.stream_sequence <= through_sequence)
        statement = statement.order_by(ReportedHealthRow.received_at, ReportedHealthRow.event_id)
        if limit is not None:
            statement = statement.limit(limit)
        rows = self._session.scalars(statement).all()
        return tuple(row.to_domain() for row in rows)

    def stream_watermarks(self) -> tuple[int, int]:
        decision_sequence = self._session.scalar(
            select(func.max(ReportedDecisionRow.stream_sequence))
        )
        health_sequence = self._session.scalar(select(func.max(ReportedHealthRow.stream_sequence)))
        return int(decision_sequence or 0), int(health_sequence or 0)


def _ensure_same(
    existing: dict[str, object], incoming: dict[str, object], kind: str, event_id: str
) -> None:
    if existing != incoming:
        raise MonitorRefusedError(
            f"{kind} event id {event_id!r} was reused with a different payload"
        )


__all__ = ["PostgresMonitorRepository"]
