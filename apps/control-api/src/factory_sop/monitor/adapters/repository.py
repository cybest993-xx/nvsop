"""monitor 幂等观测镜像的 PostgreSQL 适配器。"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    Integer,
    Table,
    Text,
    and_,
    exists,
    func,
    literal,
    or_,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy import (
    delete as sql_delete,
)
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import InstrumentedAttribute

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
    RetentionCursorRow,
    RetentionRecordArchiveRow,
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
from factory_sop.monitor.repository import MonitorImpactRepository, MonitorRepository
from factory_sop.monitor.usecases import latest_health_per_stream
from factory_sop.retention.api import (
    DisposalRetirement,
    ImpactAggregate,
    InstanceRetentionFact,
    RecordRetirement,
    RetentionCutoffs,
    RetentionSkip,
    StationUsage,
    empty_aggregate,
)
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
            return _retired_replay(
                self._session, DecisionIdentityRow, row.event_id, row.payload, "decision"
            )
        _ensure_same(existing.payload, row.payload, "decision", row.event_id)
        return False

    def upsert_execution_authority(self, value: MirroredExecutionAuthority) -> bool:
        row = ReportedExecutionAuthorityRow.from_domain(value)
        self._acquire_execution_authority_lock(row.station_id, row.host_id)
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
            return _retired_replay(
                self._session, HealthIdentityRow, row.event_id, row.payload, "health"
            )
        _ensure_same(existing.payload, row.payload, "health", row.event_id)
        return False

    def upsert_instance(self, value: MirroredSopInstance) -> bool:
        self._acquire_stream_lock(_DECISION_STREAM_LOCK_KEY)
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
        self._acquire_stream_lock(_DECISION_STREAM_LOCK_KEY)
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
        self._acquire_stream_lock(_DECISION_STREAM_LOCK_KEY)
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
        self._acquire_stream_lock(_DECISION_STREAM_LOCK_KEY)
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
            return _retired_replay(
                self._session, ObservationIdentityRow, row.event_id, row.payload, "observation"
            )
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
            select(DecisionIdentityRow.stream_sequence).where(
                DecisionIdentityRow.event_id == event_id
            )
        )

    def health_sequence_for_event(self, event_id: str) -> int | None:
        return self._session.scalar(
            select(HealthIdentityRow.stream_sequence).where(HealthIdentityRow.event_id == event_id)
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

    def _acquire_execution_authority_lock(self, station_id: str, host_id: str) -> None:
        key = f"monitor-execution-authority:{station_id}:{host_id}"
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
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


class PostgresMonitorImpactRepository(MonitorImpactRepository):
    """保留影响估算的只读计数；只聚合 monitor 自己的镜像事实，不读 evidence/retention 表。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def instance_retention_facts(
        self, *, instance_ids: tuple[tuple[UUID, UUID, int], ...]
    ) -> tuple[InstanceRetentionFact, ...]:
        """只查目标来源机/工位/实例，不能借用换机前后同编号实例的结案与类别。"""
        wanted = {(str(host), str(station), instance) for host, station, instance in instance_ids}
        if not wanted:
            return ()
        instances = self._session.scalars(
            select(ReportedSopInstanceRow).where(
                tuple_(
                    ReportedSopInstanceRow.host_id,
                    ReportedSopInstanceRow.station_id,
                    ReportedSopInstanceRow.instance_id,
                ).in_(wanted)
            )
        ).all()
        closed: dict[tuple[str, str, int], bool] = {}
        for instance_row in instances:
            key = (instance_row.host_id, instance_row.station_id, instance_row.instance_id)
            closed[key] = closed.get(key, True) and instance_row.closed_at is not None
        latched = set(
            self._session.execute(
                select(
                    ReportedViolationRow.host_id,
                    ReportedViolationRow.station_id,
                    ReportedViolationRow.instance_id,
                ).where(
                    tuple_(
                        ReportedViolationRow.host_id,
                        ReportedViolationRow.station_id,
                        ReportedViolationRow.instance_id,
                    ).in_(wanted)
                )
            )
            .tuples()
            .all()
        )
        decision_instance = cast(Any, ReportedDecisionRow.payload)["instance_id"].astext.cast(
            Integer
        )
        rows = self._session.execute(
            select(
                ReportedDecisionRow.host_id,
                ReportedDecisionRow.station_id,
                decision_instance.label("instance_id"),
                ReportedDecisionRow.payload,
            )
            .where(
                tuple_(
                    ReportedDecisionRow.host_id, ReportedDecisionRow.station_id, decision_instance
                ).in_(wanted)
            )
            .order_by(ReportedDecisionRow.received_at.desc(), ReportedDecisionRow.event_id.desc())
        ).all()
        verdicts: dict[tuple[str, str, int], str] = {}
        for row in rows:
            payload = cast(dict[str, object], row.payload)
            verdict = payload.get("verdict")
            if isinstance(verdict, str):
                verdicts.setdefault((row.host_id, row.station_id, row.instance_id), verdict)
        return tuple(
            InstanceRetentionFact(
                host_id=UUID(host),
                station_id=UUID(station),
                instance_id=instance,
                verdict=verdicts.get((host, station, instance)),
                has_latched_violation=(host, station, instance) in latched,
                closed=closed.get((host, station, instance), False),
            )
            for host, station, instance in sorted(wanted)
        )

    def record_impact(
        self,
        *,
        station_id: str | None,
        exposed_from: datetime | None,
        exposed_to: datetime,
    ) -> tuple[StationUsage, ...]:
        """判定/健康/观测记录：实例已由真实 ``closed_at`` 结案才暴露，否则继续受保护。"""
        per_station: dict[str | None, dict[bool, ImpactAggregate]] = {}
        decision_instance = cast(Any, ReportedDecisionRow.payload)["instance_id"].astext.cast(
            Integer
        )
        for table, station_expr, instance_expr in (
            (ReportedDecisionRow, ReportedDecisionRow.station_id, decision_instance),
            (
                ReportedObservationRow,
                ReportedObservationRow.station_id,
                ReportedObservationRow.instance_id,
            ),
            (ReportedHealthRow, ReportedHealthRow.station_id, None),
        ):
            protection = (
                literal(False)
                if instance_expr is None
                else ~_instance_closed(
                    host_expr=table.host_id, station_expr=station_expr, instance_expr=instance_expr
                )
            )
            self._accumulate(
                per_station,
                table=table,
                station_expr=station_expr,
                protection=protection,
                station_id=station_id,
                exposed_from=exposed_from,
                exposed_to=exposed_to,
            )
        return _usages(per_station)

    def disposal_impact(
        self,
        *,
        station_id: str | None,
        exposed_from: datetime | None,
        exposed_to: datetime,
    ) -> tuple[StationUsage, ...]:
        """处置记录会回收；无明确结案事实的已锁存违规按 §5.19 继续受保护。"""
        per_station: dict[str | None, dict[bool, ImpactAggregate]] = {}
        disposal_station = cast(Any, ReportedDisposalRow.payload)["station_id"].astext
        self._accumulate(
            per_station,
            table=ReportedDisposalRow,
            station_expr=disposal_station,
            protection=cast(Any, ReportedDisposalRow.payload)["violation_ref"].astext.is_not(None),
            station_id=station_id,
            exposed_from=exposed_from,
            exposed_to=exposed_to,
        )
        self._accumulate(
            per_station,
            table=ReportedViolationRow,
            station_expr=ReportedViolationRow.station_id,
            protection=literal(True),
            station_id=station_id,
            exposed_from=exposed_from,
            exposed_to=exposed_to,
        )
        return _usages(per_station)

    def _accumulate(
        self,
        per_station: dict[str | None, dict[bool, ImpactAggregate]],
        *,
        table: type[Any],
        station_expr: ColumnElement[Any] | InstrumentedAttribute[Any],
        protection: ColumnElement[bool],
        station_id: str | None,
        exposed_from: datetime | None,
        exposed_to: datetime,
    ) -> None:
        conditions: list[ColumnElement[bool]] = [table.received_at <= exposed_to]
        if exposed_from is not None:
            conditions.append(table.received_at > exposed_from)
        if station_id is not None:
            conditions.append(station_expr == station_id)
        statement = (
            select(
                station_expr.label("station_id"),
                protection.label("protected"),
                func.count().label("objects"),
                func.coalesce(func.sum(func.octet_length(sql_cast(table.payload, Text))), 0).label(
                    "known_bytes"
                ),
                func.min(table.received_at).label("earliest"),
                func.max(table.received_at).label("latest"),
            )
            .where(*conditions)
            .group_by(station_expr, protection)
        )
        for row in self._session.execute(statement).all():
            raw_station = row.station_id
            key = None if raw_station is None else str(raw_station)
            aggregate = ImpactAggregate(
                objects=int(row.objects),
                known_bytes=int(row.known_bytes),
                unknown_size_objects=0,
                earliest=cast("datetime | None", row.earliest),
                latest=cast("datetime | None", row.latest),
            )
            split = per_station.setdefault(key, {})
            bucket = bool(row.protected)
            split[bucket] = aggregate if bucket not in split else split[bucket].plus(aggregate)


class PostgresMonitorRetirementRepository:
    """每表最多检查 limit 条；游标推进包含保护跳过，避免旧保护对象饿死后续候选。"""

    def __init__(
        self,
        session: Session,
        evidence_protected: Callable[[str, str, int], bool],
    ) -> None:
        self._session = session
        self._evidence_protected = evidence_protected

    def _page(
        self,
        table: type[Any],
        station: ColumnElement[Any] | InstrumentedAttribute[Any],
        cutoffs: RetentionCutoffs,
        limit: int,
        kind: str,
    ) -> Sequence[Any]:
        custom = [value.station_id for value in cutoffs.cutoffs if value.station_id is not None]
        scopes = []
        for value in cutoffs.cutoffs:
            if value.moment is None:
                continue
            scope = (
                station.not_in(custom) if value.station_id is None else station == value.station_id
            )
            scopes.append(and_(scope, table.received_at <= value.moment))
        if not scopes:
            return ()
        cursor = self._session.get(RetentionCursorRow, kind)
        if cursor is None:
            cursor = RetentionCursorRow(kind=kind)
            self._session.add(cursor)
        conditions = [or_(*scopes)]
        if cursor.received_at is not None:
            conditions.append(
                tuple_(table.received_at, table.event_id)
                > tuple_(literal(cursor.received_at), literal(cursor.event_id))
            )
        rows = self._session.scalars(
            select(table)
            .where(*conditions)
            .order_by(table.received_at, table.event_id)
            .limit(limit)
        ).all()
        cursor.received_at = rows[-1].received_at if len(rows) == limit else None
        cursor.event_id = rows[-1].event_id if len(rows) == limit else None
        self._session.flush()
        return rows

    def _protection(self, host: str, station: str, instance: int | None) -> str | None:
        if instance is None or not self._session.scalar(
            select(
                _instance_closed(
                    host_expr=literal(host),
                    station_expr=literal(station),
                    instance_expr=literal(instance),
                )
            )
        ):
            return "instance_open_or_unknown"
        violation = self._session.scalar(
            select(ReportedViolationRow.event_id)
            .where(
                ReportedViolationRow.host_id == host,
                ReportedViolationRow.station_id == station,
                ReportedViolationRow.instance_id == instance,
            )
            .limit(1)
        )
        if violation is not None:
            return "violation_latched"
        if self._evidence_protected(host, station, instance):
            return "evidence_reference_retained"
        return None

    def retire_records(
        self, *, cutoffs: RetentionCutoffs, limit: int, now: datetime
    ) -> RecordRetirement:
        del now
        self._lock_streams()
        deleted = archived = 0
        buckets: Counter[tuple[str, str, int | None, datetime, str, str]] = Counter()
        moments: list[datetime] = []
        skips: Counter[str] = Counter()
        for table, kind, identity in (
            (ReportedDecisionRow, "decision", DecisionIdentityRow),
            (ReportedObservationRow, "observation", ObservationIdentityRow),
            (ReportedHealthRow, "health", HealthIdentityRow),
        ):
            page = self._page(table, table.station_id, cutoffs, limit, kind)
            for row in page:
                moments.append(row.received_at)
                instance = row.payload.get("instance_id")
                reason = (
                    None
                    if kind == "health"
                    else self._protection(row.host_id, row.station_id, instance)
                )
                if reason is not None:
                    skips[reason] += 1
                    continue
                buckets[
                    (
                        row.station_id,
                        row.host_id,
                        instance,
                        row.received_at.replace(minute=0, second=0, microsecond=0),
                        row.payload.get("verdict", "unknown"),
                        kind,
                    )
                ] += 1
                # 保留全局身份与载荷摘要，既拒绝冲突重放，也不让旧事件重新生成为明细。
                self._session.execute(
                    update(identity)
                    .where(identity.event_id == row.event_id)
                    .values(retired_payload_sha256=_payload_digest(row.payload))
                )
                self._session.execute(
                    sql_delete(table).where(
                        table.event_id == row.event_id, table.received_at == row.received_at
                    )
                )
                deleted += 1
        archived = self._archive(buckets)
        return RecordRetirement(
            deleted, archived, min(moments, default=None), max(moments, default=None), _skips(skips)
        )

    def retire_disposals(
        self, *, cutoffs: RetentionCutoffs, limit: int, now: datetime
    ) -> DisposalRetirement:
        del now
        self._lock_streams()
        station = ReportedDisposalRow.payload["station_id"].astext
        page = self._page(ReportedDisposalRow, station, cutoffs, limit, "disposal")
        skips: Counter[str] = Counter()
        deleted = 0
        for row in page:
            # 未知或违规关联处置继续保留；不从设备写入成功推断违规已结案。
            instance = row.payload.get("instance_id")
            source_station = row.payload.get("station_id")
            reason: str | None = "instance_open_or_unknown"
            if isinstance(instance, int) and isinstance(source_station, str):
                reason = self._protection(row.host_id, source_station, instance)
            # 当前处置契约必带 violation_ref，且没有违规结案权威；成功处置不解除保护。
            skips[reason or "violation_closure_unknown"] += 1
        moments = [row.received_at for row in page]
        return DisposalRetirement(
            deleted, 0, min(moments, default=None), max(moments, default=None), _skips(skips)
        )

    def _lock_streams(self) -> None:
        # 与镜像写入共用提交顺序锁；保护判定、归档、删除和身份墓碑是同一临界区。
        for key in (_DECISION_STREAM_LOCK_KEY, _HEALTH_STREAM_LOCK_KEY):
            self._session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    def _archive(self, buckets: Counter[tuple[str, str, int | None, datetime, str, str]]) -> int:
        for (station, host, instance, bucket, verdict, kind), count in buckets.items():
            self._session.add(
                RetentionRecordArchiveRow(
                    station_id=station,
                    host_id=host,
                    instance_id=instance,
                    bucket_start=bucket,
                    verdict=verdict,
                    decisions=count if kind == "decision" else 0,
                    observations=count if kind == "observation" else 0,
                    health=count if kind == "health" else 0,
                    disposals=0,
                )
            )
        self._session.flush()
        return len(buckets)


def _skips(counts: Counter[str]) -> tuple[RetentionSkip, ...]:
    return tuple(RetentionSkip(reason, count) for reason, count in sorted(counts.items()))


def _payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _retired_replay(
    session: Session, identity: type[Any], event_id: str, payload: dict[str, Any], kind: str
) -> bool:
    retired = session.get(identity, event_id)
    if retired is None or retired.retired_payload_sha256 is None:
        raise RuntimeError(f"{kind} mirror conflicted without a visible row")
    if retired.retired_payload_sha256 != _payload_digest(payload):
        raise MonitorRefusedError(f"{kind} event {event_id} conflicts with retired payload")
    return False


def _instance_closed(
    *,
    host_expr: ColumnElement[Any] | InstrumentedAttribute[Any],
    station_expr: ColumnElement[Any] | InstrumentedAttribute[Any],
    instance_expr: ColumnElement[Any] | InstrumentedAttribute[Any],
) -> ColumnElement[bool]:
    """该记录所属实例是否已由真实 ``closed_at`` 结案；实例未知即未结案，继续受保护。"""
    closed = (
        select(
            ReportedSopInstanceRow.host_id,
            ReportedSopInstanceRow.station_id,
            ReportedSopInstanceRow.instance_id,
        )
        .where(ReportedSopInstanceRow.closed_at.is_not(None))
        .subquery()
    )
    return exists(
        select(literal(1))
        .select_from(closed)
        .where(
            and_(
                closed.c.host_id == host_expr,
                closed.c.station_id == station_expr,
                closed.c.instance_id == instance_expr,
            )
        )
    )


def _usages(
    per_station: dict[str | None, dict[bool, ImpactAggregate]],
) -> tuple[StationUsage, ...]:
    empty = empty_aggregate(measurable_bytes=True)
    return tuple(
        StationUsage(
            station_id=None if station is None else UUID(station),
            exposed=split.get(False, empty),
            protected=split.get(True, empty),
        )
        for station, split in sorted(
            per_station.items(), key=lambda item: (item[0] is None, item[0] or "")
        )
    )


__all__ = [
    "MONITOR_STREAM_CHANNEL",
    "PostgresMonitorImpactRepository",
    "PostgresMonitorRepository",
    "PostgresMonitorRetirementRepository",
]
