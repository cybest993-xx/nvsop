"""monitor 幂等观测镜像的 PostgreSQL 适配器。"""

from __future__ import annotations

from typing import cast

from sqlalchemy import Table, func, select, text, update
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
from nvsop_contracts import ReportedSopInstance

MONITOR_STREAM_CHANNEL = "nvsop_monitor_stream"
_DECISION_STREAM_LOCK_KEY = 0x4D4F4E444543  # "MONDEC"
_HEALTH_STREAM_LOCK_KEY = 0x4D4F4E484C54  # "MONHLT"


class PostgresMonitorRepository(MonitorRepository):
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_decision(self, value: MirroredDecision) -> bool:
        self._acquire_stream_lock(_DECISION_STREAM_LOCK_KEY)
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
            self._notify_stream("decision")
            return True
        existing = self._session.get(ReportedDecisionRow, row.event_id)
        if existing is None:
            raise RuntimeError("decision mirror insert conflicted without a visible row")
        _ensure_same(existing.payload, row.payload, "decision", row.event_id)
        return False

    def upsert_health(self, value: MirroredHealth) -> bool:
        self._acquire_stream_lock(_HEALTH_STREAM_LOCK_KEY)
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
            self._notify_stream("health")
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
            open_boundary_signal=row.open_boundary_signal,
            close_boundary_signal=row.close_boundary_signal,
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
        existing = self._session.get(ReportedSopInstanceRow, row.event_id)
        if existing is None:
            raise RuntimeError("instance mirror upsert completed without a visible row")
        existing_report = existing.to_domain().report
        incoming_report = value.report
        if existing_report.closed_at is None and incoming_report.closed_at is not None:
            _ensure_instance_progression(existing_report, incoming_report)
            closed = self._session.execute(
                update(ReportedSopInstanceRow)
                .where(
                    ReportedSopInstanceRow.event_id == row.event_id,
                    ReportedSopInstanceRow.closed_at.is_(None),
                )
                .values(
                    closed_at=row.closed_at,
                    close_reason=row.close_reason,
                    close_boundary_signal=row.close_boundary_signal,
                    received_at=row.received_at,
                    payload=row.payload,
                )
                .returning(ReportedSopInstanceRow.event_id)
            )
            if closed.scalar_one_or_none() is not None:
                return True
            current = self._session.get(ReportedSopInstanceRow, row.event_id)
            if current is None:
                raise RuntimeError("instance mirror close raced without a visible row")
            _ensure_same(current.payload, row.payload, "instance", row.event_id)
            return False
        if existing_report.closed_at is not None and incoming_report.closed_at is None:
            _ensure_instance_progression(incoming_report, existing_report)
            return False
        _ensure_same(existing.payload, row.payload, "instance", row.event_id)
        return False

    def page_instances(
        self, *, page: int, page_size: int
    ) -> tuple[tuple[MirroredSopInstance, ...], int]:
        rows = self._session.scalars(
            select(ReportedSopInstanceRow)
            .order_by(
                ReportedSopInstanceRow.received_at.desc(),
                ReportedSopInstanceRow.event_id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        total = self._session.scalar(select(func.count()).select_from(ReportedSopInstanceRow))
        return tuple(row.to_domain() for row in rows), int(total or 0)

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

    def _acquire_stream_lock(self, key: int) -> None:
        # Identity 在 INSERT 时取序号；先按流串行化事务，才能让序号顺序等于提交可见顺序。
        self._session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    def _notify_stream(self, kind: str) -> None:
        # pg_notify 是事务性的：只有本次镜像提交成功后 listener 才能收到提示。
        self._session.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": MONITOR_STREAM_CHANNEL, "payload": kind},
        )


def _ensure_same(
    existing: dict[str, object], incoming: dict[str, object], kind: str, event_id: str
) -> None:
    if existing != incoming:
        raise MonitorRefusedError(
            f"{kind} event id {event_id!r} was reused with a different payload"
        )


def _ensure_instance_progression(opened: ReportedSopInstance, closed: ReportedSopInstance) -> None:
    if opened.closed_at is not None or closed.closed_at is None:
        raise MonitorRefusedError("instance lifecycle transition must be open to closed")
    immutable_open = (
        opened.event_id,
        opened.trace_id,
        opened.host_id,
        opened.station_id,
        opened.instance_id,
        opened.opened_at,
        opened.open_boundary_signal,
        opened.template_version_id,
        opened.template_sha256,
        opened.configuration_revision,
        opened.configuration_sha256,
        opened.contract_version,
    )
    immutable_closed = (
        closed.event_id,
        closed.trace_id,
        closed.host_id,
        closed.station_id,
        closed.instance_id,
        closed.opened_at,
        closed.open_boundary_signal,
        closed.template_version_id,
        closed.template_sha256,
        closed.configuration_revision,
        closed.configuration_sha256,
        closed.contract_version,
    )
    if immutable_open != immutable_closed:
        raise MonitorRefusedError("instance event reused with different immutable provenance")
    opened_provenance = {item.backend_id: item.model_ids for item in opened.backend_provenance}
    closed_provenance = {item.backend_id: item.model_ids for item in closed.backend_provenance}
    if any(closed_provenance.get(key) != value for key, value in opened_provenance.items()):
        raise MonitorRefusedError("instance provenance cannot remove or rewrite observed sources")


__all__ = ["MONITOR_STREAM_CHANNEL", "PostgresMonitorRepository"]
