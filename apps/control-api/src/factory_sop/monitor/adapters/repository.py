"""monitor 幂等观测镜像的 PostgreSQL 适配器。"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import Table, and_, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.orm import Session

from factory_sop.monitor.adapters.tables import (
    DecisionIdentityRow,
    HealthIdentityRow,
    ObservationIdentityRow,
    ReportedDecisionRow,
    ReportedDisposalRow,
    ReportedExecutionAuthorityRow,
    ReportedHealthRow,
    ReportedObservationRow,
    ReportedSopInstanceRow,
    ReportedViolationRow,
)
from factory_sop.monitor.errors import MonitorRefusedError
from factory_sop.monitor.model import (
    MirroredDecision,
    MirroredExecutionAuthority,
    MirroredHealth,
    MirroredObservation,
    MirroredSopInstance,
    MirroredViolation,
)
from factory_sop.monitor.repository import MonitorRepository
from factory_sop.monitor.usecases import latest_health_per_stream
from nvsop_contracts import (
    ReportedDisposal,
    ReportedSopInstance,
    reported_decision_to_wire,
    reported_health_to_wire,
    reported_observation_to_wire,
    reported_sop_instance_to_wire,
)

MONITOR_STREAM_CHANNEL = "nvsop_monitor_stream"
_DECISION_STREAM_LOCK_KEY = 0x4D4F4E444543  # "MONDEC"
_HEALTH_STREAM_LOCK_KEY = 0x4D4F4E484C54  # "MONHLT"


class PostgresMonitorRepository(MonitorRepository):
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_decision(self, value: MirroredDecision) -> bool:
        self._acquire_stream_lock(_DECISION_STREAM_LOCK_KEY)
        row = ReportedDecisionRow.from_domain(value)
        identity = cast(Table, DecisionIdentityRow.__table__)
        # 先按事件 id 预约全局唯一身份并取流内序号，再写分区事实；重复上报不写事实。
        sequence = self._session.execute(
            postgres_insert(identity)
            .values(event_id=row.event_id, received_at=row.received_at)
            .on_conflict_do_nothing(index_elements=[identity.c.event_id])
            .returning(identity.c.stream_sequence)
        ).scalar_one_or_none()
        table = cast(Table, ReportedDecisionRow.__table__)
        if sequence is not None:
            self._session.execute(
                postgres_insert(table).values(
                    event_id=row.event_id,
                    received_at=row.received_at,
                    stream_sequence=sequence,
                    trace_id=row.trace_id,
                    host_id=row.host_id,
                    station_id=row.station_id,
                    backend_id=row.backend_id,
                    payload=row.payload,
                    latched_at=row.latched_at,
                    realtime=row.realtime,
                )
            )
            self._notify_stream("decision")
            return True
        existing = self._session.scalars(
            select(ReportedDecisionRow).where(ReportedDecisionRow.event_id == row.event_id)
        ).one_or_none()
        if existing is None:
            raise RuntimeError("decision mirror conflicted without a visible row")
        _ensure_same(existing.payload, row.payload, "decision", row.event_id)
        return False

    def upsert_execution_authority(self, value: MirroredExecutionAuthority) -> bool:
        row = ReportedExecutionAuthorityRow.from_domain(value)
        existing = self._session.get(
            ReportedExecutionAuthorityRow,
            {"station_id": row.station_id, "host_id": row.host_id},
        )
        if existing is None:
            self._session.add(row)
            self._session.flush()
            self._notify_stream("runtime")
            return True
        if row.reported_at < existing.reported_at:
            return False
        if row.reported_at == existing.reported_at:
            _ensure_same(existing.payload, row.payload, "execution authority", row.station_id)
            return False
        previous_semantic = {k: v for k, v in existing.payload.items() if k != "reported_at"}
        current_semantic = {k: v for k, v in row.payload.items() if k != "reported_at"}
        existing.reported_at = row.reported_at
        existing.received_at = row.received_at
        existing.payload = row.payload
        self._session.flush()
        if previous_semantic != current_semantic:
            self._notify_stream("runtime")
        return True

    def upsert_health(self, value: MirroredHealth) -> bool:
        self._acquire_stream_lock(_HEALTH_STREAM_LOCK_KEY)
        row = ReportedHealthRow.from_domain(value)
        identity = cast(Table, HealthIdentityRow.__table__)
        sequence = self._session.execute(
            postgres_insert(identity)
            .values(event_id=row.event_id, received_at=row.received_at)
            .on_conflict_do_nothing(index_elements=[identity.c.event_id])
            .returning(identity.c.stream_sequence)
        ).scalar_one_or_none()
        table = cast(Table, ReportedHealthRow.__table__)
        if sequence is not None:
            self._session.execute(
                postgres_insert(table).values(
                    event_id=row.event_id,
                    received_at=row.received_at,
                    stream_sequence=sequence,
                    trace_id=row.trace_id,
                    host_id=row.host_id,
                    station_id=row.station_id,
                    stream_id=row.stream_id,
                    payload=row.payload,
                )
            )
            self._notify_stream("health")
            return True
        existing = self._session.scalars(
            select(ReportedHealthRow).where(ReportedHealthRow.event_id == row.event_id)
        ).one_or_none()
        if existing is None:
            raise RuntimeError("health mirror conflicted without a visible row")
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
            self._notify_stream("runtime")
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
                self._notify_stream("runtime")
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

    def upsert_violation(self, value: MirroredViolation) -> bool:
        row = ReportedViolationRow.from_domain(value)
        table = cast(Table, ReportedViolationRow.__table__)
        statement = postgres_insert(table).values(
            event_id=row.event_id,
            decision_event_id=row.decision_event_id,
            host_id=row.host_id,
            station_id=row.station_id,
            instance_id=row.instance_id,
            reason_code=row.reason_code,
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
        existing = self._session.get(ReportedViolationRow, row.event_id)
        if existing is None:
            raise RuntimeError("violation mirror insert conflicted without a visible row")
        _ensure_same(existing.payload, row.payload, "violation", row.event_id)
        return False

    def page_violations(
        self, *, page: int, page_size: int
    ) -> tuple[tuple[MirroredViolation, ...], int]:
        rows = self._session.scalars(
            select(ReportedViolationRow)
            .order_by(
                ReportedViolationRow.received_at.desc(),
                ReportedViolationRow.event_id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        total = self._session.scalar(select(func.count()).select_from(ReportedViolationRow))
        return tuple(row.to_domain() for row in rows), int(total or 0)

    def upsert_disposal(self, report: ReportedDisposal, *, received_at: datetime) -> bool:
        payload = report.to_wire()
        table = cast(Table, ReportedDisposalRow.__table__)
        result = self._session.execute(
            postgres_insert(table)
            .values(
                event_id=report.event_id,
                host_id=report.host_id,
                received_at=received_at,
                payload=payload,
            )
            .on_conflict_do_nothing(index_elements=[table.c.event_id])
            .returning(table.c.event_id)
        )
        if result.scalar_one_or_none() is not None:
            return True
        existing = self._session.get(ReportedDisposalRow, report.event_id)
        if existing is None:
            raise RuntimeError("disposal mirror insert conflicted without a visible row")
        _ensure_same(existing.payload, payload, "disposal", report.event_id)
        return False

    def page_disposals(
        self, *, page: int, page_size: int
    ) -> tuple[tuple[ReportedDisposal, ...], int]:
        rows = self._session.scalars(
            select(ReportedDisposalRow)
            .order_by(ReportedDisposalRow.received_at.desc(), ReportedDisposalRow.event_id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        total = self._session.scalar(select(func.count()).select_from(ReportedDisposalRow))
        return tuple(
            ReportedDisposal.from_wire(cast(dict[str, object], row.payload)) for row in rows
        ), int(total or 0)

    def upsert_observation(self, value: MirroredObservation) -> bool:
        row = ReportedObservationRow.from_domain(value)
        identity = cast(Table, ObservationIdentityRow.__table__)
        reserved = self._session.execute(
            postgres_insert(identity)
            .values(event_id=row.event_id, received_at=row.received_at)
            .on_conflict_do_nothing(index_elements=[identity.c.event_id])
            .returning(identity.c.event_id)
        ).scalar_one_or_none()
        table = cast(Table, ReportedObservationRow.__table__)
        if reserved is not None:
            self._session.execute(
                postgres_insert(table).values(
                    event_id=row.event_id,
                    received_at=row.received_at,
                    trace_id=row.trace_id,
                    host_id=row.host_id,
                    station_id=row.station_id,
                    instance_id=row.instance_id,
                    source=row.source,
                    signal=row.signal,
                    observed_at=row.observed_at,
                    payload=row.payload,
                )
            )
            self._notify_stream("runtime")
            return True
        existing = self._session.scalars(
            select(ReportedObservationRow).where(ReportedObservationRow.event_id == row.event_id)
        ).one_or_none()
        if existing is None:
            raise RuntimeError("observation mirror conflicted without a visible row")
        _ensure_same(existing.payload, row.payload, "observation", row.event_id)
        return False

    def page_observations(
        self,
        *,
        page: int,
        page_size: int,
        station_id: str | None = None,
        instance_id: int | None = None,
    ) -> tuple[tuple[MirroredObservation, ...], int]:
        filters = []
        if station_id is not None:
            filters.append(ReportedObservationRow.station_id == station_id)
        if instance_id is not None:
            filters.append(ReportedObservationRow.instance_id == instance_id)
        rows = self._session.scalars(
            select(ReportedObservationRow)
            .where(*filters)
            .order_by(
                ReportedObservationRow.received_at.desc(),
                ReportedObservationRow.event_id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        total = self._session.scalar(
            select(func.count()).select_from(ReportedObservationRow).where(*filters)
        )
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

    def execution_authority_for_stations(
        self, *, station_ids: tuple[str, ...]
    ) -> tuple[MirroredExecutionAuthority, ...]:
        if not station_ids:
            return ()
        rows = self._session.scalars(
            select(ReportedExecutionAuthorityRow).where(
                ReportedExecutionAuthorityRow.station_id.in_(station_ids)
            )
        ).all()
        return tuple(row.to_domain() for row in rows)

    def health_for_station(self, *, station_id: str) -> tuple[MirroredHealth, ...]:
        rows = self._session.scalars(
            select(ReportedHealthRow).where(ReportedHealthRow.station_id == station_id)
        ).all()
        return tuple(row.to_domain() for row in rows)

    def last_report_at_by_host(self) -> tuple[tuple[str, datetime], ...]:
        """跨全部镜像事实汇总每个主机最近一次被中心接收的时刻。"""
        latest: dict[str, datetime] = {}
        for host_column, received_column in (
            (ReportedDecisionRow.host_id, ReportedDecisionRow.received_at),
            (ReportedDisposalRow.host_id, ReportedDisposalRow.received_at),
            (ReportedExecutionAuthorityRow.host_id, ReportedExecutionAuthorityRow.received_at),
            (ReportedHealthRow.host_id, ReportedHealthRow.received_at),
            (ReportedObservationRow.host_id, ReportedObservationRow.received_at),
            (ReportedSopInstanceRow.host_id, ReportedSopInstanceRow.received_at),
        ):
            rows = self._session.execute(
                select(host_column, func.max(received_column)).group_by(host_column)
            ).all()
            for host_id, received_at in rows:
                current = latest.get(host_id)
                if current is None or received_at > current:
                    latest[host_id] = received_at
        return tuple(sorted(latest.items()))

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

    def runtime_projection(self) -> tuple[dict[str, object], ...]:
        """从 durable mirror 按工位读取当前事实，不限制全局行数。

        每路流健康复用 monitor owner 的事件时间分类，避免 Center ingestion 序号在
        迟到旧报告时覆盖较新的业务事实。
        """
        decisions = self._session.scalars(
            select(ReportedDecisionRow)
            .distinct(ReportedDecisionRow.station_id)
            .order_by(ReportedDecisionRow.station_id, ReportedDecisionRow.stream_sequence.desc())
        ).all()
        health = self._session.scalars(
            select(ReportedHealthRow)
            .where(ReportedHealthRow.station_id.is_not(None))
            .order_by(ReportedHealthRow.station_id, ReportedHealthRow.stream_id)
        ).all()
        latest_hosts = (
            select(ReportedSopInstanceRow.station_id, ReportedSopInstanceRow.host_id)
            .distinct(ReportedSopInstanceRow.station_id)
            .order_by(
                ReportedSopInstanceRow.station_id,
                ReportedSopInstanceRow.received_at.desc(),
                ReportedSopInstanceRow.event_id.desc(),
            )
            .subquery()
        )
        instances = self._session.scalars(
            select(ReportedSopInstanceRow)
            .join(
                latest_hosts,
                and_(
                    ReportedSopInstanceRow.station_id == latest_hosts.c.station_id,
                    ReportedSopInstanceRow.host_id == latest_hosts.c.host_id,
                ),
            )
            .distinct(ReportedSopInstanceRow.station_id)
            .order_by(
                ReportedSopInstanceRow.station_id,
                ReportedSopInstanceRow.instance_id.desc(),
                ReportedSopInstanceRow.event_id.desc(),
            )
        ).all()
        observations = self._session.scalars(
            select(ReportedObservationRow)
            .distinct(ReportedObservationRow.station_id)
            .order_by(
                ReportedObservationRow.station_id,
                ReportedObservationRow.received_at.desc(),
                ReportedObservationRow.event_id.desc(),
            )
        ).all()
        by_station: dict[str, dict[str, object]] = {}
        health_by_station: dict[str, list[MirroredHealth]] = {}
        for decision_row in decisions:
            by_station.setdefault(decision_row.station_id, {})["decision"] = (
                reported_decision_to_wire(decision_row.to_domain().report)
            )
        for health_row in health:
            health_by_station.setdefault(health_row.station_id or "", []).append(
                health_row.to_domain()
            )
        for station_id, reports in health_by_station.items():
            by_station.setdefault(station_id, {})["health"] = [
                reported_health_to_wire(value.report)
                for value in latest_health_per_stream(tuple(reports))
            ]
        for instance_row in instances:
            by_station.setdefault(instance_row.station_id, {})["instance"] = (
                reported_sop_instance_to_wire(instance_row.to_domain().report)
            )
        for observation_row in observations:
            by_station.setdefault(observation_row.station_id, {})["observation"] = (
                reported_observation_to_wire(observation_row.to_domain().report)
            )
        return tuple(
            {"station_id": station_id, **by_station[station_id]}
            for station_id in sorted(by_station)
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
