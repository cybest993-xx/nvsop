"""#207 回收执行：真实 PostgreSQL 上的有界回收、保护跳过、失败重跑与聚合归档。

retention 解析策略并记周期账本；monitor 在同一事务内保护引用、聚合归档与删除明细。
真实 PostgreSQL + Edge 临时目录的跨端场景在 test_retention_sweep_edge_bridge 中。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from factory_sop.identifiers import new_id
from factory_sop.monitor.adapters.repository import (
    PostgresMonitorRepository,
)
from factory_sop.monitor.model import (
    MirroredDecision,
    MirroredObservation,
    MirroredSopInstance,
    MirroredViolation,
)
from factory_sop.monitor.sweep import RepositoryMonitorRetirement
from factory_sop.persistence import session_factory as make_session_factory
from factory_sop.retention.adapters.repository import (
    PostgresRetentionPolicyRepository,
    PostgresStationRetentionOverrideRepository,
)
from factory_sop.retention.adapters.sweep_repository import PostgresRetentionSweepRepository
from factory_sop.retention.api import (
    DEFAULT_RETENTION_POLICY,
    DisposalRetirement,
    RecordRetirement,
    RetentionMode,
    RetentionRule,
    RetentionSweepResult,
    run_retention_sweep,
)
from factory_sop.retention.model import RetentionPolicyMode
from factory_sop.retention.usecases import StationRetentionRecord
from nvsop_contracts import (
    ReportBackendProvenance,
    ReportedDecision,
    ReportedObservation,
    ReportedSopInstance,
    ReportEvidence,
    ReportViolation,
)

_SWEEP_TABLES = (
    "retention_sweep",
    "monitor_record_archive",
    "monitor_retention_cursor",
    "monitor_reported_decision",
    "monitor_decision_identity",
    "monitor_observation",
    "monitor_observation_identity",
    "monitor_reported_health",
    "monitor_health_identity",
    "monitor_disposal",
    "monitor_violation",
    "monitor_sop_instance",
    "retention_station_policy",
    "retention_policy",
)


@pytest.fixture
def committed_session(engine: Engine) -> Iterator[Session]:
    """种子与执行结果真实提交；用例结束后只清理本场景拥有的表。"""
    factory = make_session_factory(engine)
    opened = factory()
    try:
        yield opened
    finally:
        opened.close()
        with engine.begin() as connection:
            for table in _SWEEP_TABLES:
                connection.execute(text(f"DELETE FROM {table}"))


HOST = "00000000-0000-4000-8000-000000000001"
NOW = datetime(2026, 10, 14, tzinfo=UTC)
_DAY = 24 * 60 * 60
SWEEP_INTERVAL = 600


def _station_text() -> str:
    return str(new_id())


def _instance(
    station_id: str,
    *,
    instance_id: int,
    received_at: datetime,
    closed: bool,
    host_id: str = HOST,
) -> MirroredSopInstance:
    return MirroredSopInstance(
        report=ReportedSopInstance(
            event_id=f"instance/{host_id}/{instance_id}",
            trace_id=f"instance/{instance_id}",
            host_id=host_id,
            station_id=station_id,
            instance_id=instance_id,
            opened_at=1.0,
            closed_at=2.0 if closed else None,
            close_reason="closed_by_idle_timeout" if closed else None,
            open_boundary_signal="start",
            close_boundary_signal=None,
            template_version_id=None,
            template_sha256=None,
            backend_provenance=(ReportBackendProvenance(str(new_id()), ("model",)),),
            configuration_revision=1,
            configuration_sha256="b" * 64,
            reported_at="2026-10-01T00:00:00Z",
        ),
        received_at=received_at,
    )


def _decision(
    station_id: str,
    *,
    instance_id: int,
    received_at: datetime,
    verdict: str = "pass",
    host_id: str = HOST,
) -> MirroredDecision:
    return MirroredDecision(
        report=ReportedDecision(
            event_id=f"decision/{host_id}/{instance_id}",
            trace_id="trace",
            host_id=host_id,
            station_id=station_id,
            backend_id=str(new_id()),
            instance_id=instance_id,
            verdict=verdict,
            reason_codes=() if verdict == "pass" else ("MISSED_STEP",),
            violations=(),
            lifecycle="closed",
            evidence=ReportEvidence(None, None, None),
            template_version_id=None,
            template_sha256=None,
            model_ids=("model",),
            reported_at="2026-10-01T00:00:00Z",
        ),
        received_at=received_at,
    )


def _observation(
    station_id: str,
    *,
    instance_id: int,
    received_at: datetime,
    event_suffix: str = "",
    host_id: str = HOST,
) -> MirroredObservation:
    return MirroredObservation(
        report=ReportedObservation(
            event_id=f"observation/{host_id}/{instance_id}{event_suffix}",
            trace_id="trace",
            host_id=host_id,
            station_id=station_id,
            instance_id=instance_id,
            source="action",
            signal="(1) action",
            source_time=1.0,
            source_anchor=10.0,
            observed_at=2.0,
            template_version_id=None,
            template_sha256=None,
            backend=None,
            reported_at="2026-10-01T00:00:00Z",
        ),
        received_at=received_at,
    )


def _seed(
    session: Session,
    station_id: str,
    *,
    instance_id: int,
    received_at: datetime,
    closed: bool = True,
    verdict: str | None = "pass",
    latched: bool = False,
    observations: int = 1,
) -> None:
    monitor = PostgresMonitorRepository(session)
    monitor.upsert_instance(
        _instance(
            station_id,
            instance_id=instance_id,
            received_at=received_at,
            closed=closed,
        )
    )
    if verdict is not None:
        monitor.upsert_decision(
            _decision(
                station_id,
                instance_id=instance_id,
                received_at=received_at,
                verdict=verdict,
            )
        )
    if latched:
        monitor.upsert_violation(
            MirroredViolation(
                event_id=f"violation/{HOST}/{instance_id}",
                decision_event_id=f"decision/{HOST}/{instance_id}",
                host_id=HOST,
                station_id=station_id,
                instance_id=instance_id,
                report=ReportViolation(
                    reason_code="MISSED_STEP",
                    detail=None,
                    step_ids=(),
                    evidence=ReportEvidence(None, None, None),
                ),
                decision_reported_at="2026-10-01T00:00:00Z",
                received_at=received_at,
            )
        )
    for index in range(observations):
        monitor.upsert_observation(
            _observation(
                station_id,
                instance_id=instance_id,
                received_at=received_at,
                event_suffix=f"/{index}",
            )
        )


def _counts(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


def _executor(session: Session) -> RepositoryMonitorRetirement:
    from factory_sop.evidence.adapters.repository import PostgresEvidenceRecordProtection

    return RepositoryMonitorRetirement(
        session=session,
        evidence_protected=PostgresEvidenceRecordProtection(session).retained,
    )


def _run_sweep(
    session: Session, now: datetime = NOW, *, engine: Engine | None = None
) -> RetentionSweepResult | None:
    assert engine is not None
    return run_retention_sweep(
        policies=PostgresRetentionPolicyRepository(session),
        records=PostgresStationRetentionOverrideRepository(session),
        ledger=PostgresRetentionSweepRepository(session),
        executor=_executor(session),
        now=now,
        interval_seconds=SWEEP_INTERVAL,
    )


def test_expired_closed_records_are_archived_then_deleted(
    committed_session: Session, engine: Engine
) -> None:
    station = _station_text()
    old = NOW - timedelta(days=200)
    _seed(committed_session, station, instance_id=1, received_at=old)
    committed_session.commit()
    result = _run_sweep(committed_session, engine=engine)
    assert result is not None
    assert result.completed is True
    assert result.records is not None
    assert result.records.deleted >= 2  # 判定 + 观测
    assert result.records.archived_buckets == 2
    committed_session.commit()
    assert _counts(engine, "monitor_reported_decision") == 0
    assert _counts(engine, "monitor_observation") == 0
    assert _counts(engine, "monitor_decision_identity") == 1
    # 必要聚合归档保留：同键判定/观测计数可查。
    with engine.connect() as connection:
        archived = connection.execute(
            text("SELECT station_id, decisions, observations FROM monitor_record_archive")
        ).all()
    assert len(archived) == 2  # 判定(pass)与观测(unknown 结果)各占一个结果桶。
    assert sum(row.decisions for row in archived) == 1
    assert sum(row.observations for row in archived) == 1
    # 实例身份不删（引用保护锚点仍可查）。
    assert _counts(engine, "monitor_sop_instance") == 1
    # 账本记录范围与计数。
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT status, result, failure_code FROM retention_sweep")
        ).one()
    assert row.status == "completed"
    assert row.failure_code is None
    assert row.result["records"]["deleted"] >= 2


def test_open_or_unknown_instances_are_protected_with_reasons(
    committed_session: Session,
    engine: Engine,
) -> None:
    """未结案实例的明细记录不删并给出跳过原因；违规归档本身永久保留。"""
    station = _station_text()
    old = NOW - timedelta(days=200)
    _seed(committed_session, station, instance_id=7, received_at=old, closed=False)
    _seed(committed_session, station, instance_id=8, received_at=old, latched=True)
    committed_session.commit()
    result = _run_sweep(committed_session, engine=engine)
    assert result is not None
    assert result.completed
    assert result.records is not None
    # 未结案与锁存违规均不因年龄解除保护。
    assert result.records.deleted == 0
    assert any(
        value.reason == "instance_open_or_unknown" and value.count >= 2
        for value in result.records.skips
    )
    committed_session.commit()
    # 实例 7（未结案）的判定与观测保留。
    with engine.connect() as connection:
        remaining = connection.execute(
            text(
                "SELECT payload->>'instance_id' FROM monitor_reported_decision "
                "UNION SELECT instance_id::text FROM monitor_observation"
            )
        ).scalars()
    assert set(remaining) == {"7", "8"}
    # 违规归档永不删除（§5.19 违规与处置默认永久）。
    assert _counts(engine, "monitor_violation") == 1


def test_same_cycle_does_not_run_twice_and_next_cycle_reruns(
    committed_session: Session,
    engine: Engine,
) -> None:
    station = _station_text()
    old = NOW - timedelta(days=200)
    _seed(committed_session, station, instance_id=9, received_at=old)
    committed_session.commit()
    first = _run_sweep(committed_session, engine=engine)
    second = _run_sweep(committed_session, engine=engine)
    assert first is not None
    assert second is None
    committed_session.commit()
    # 同周期重入不重复删除。
    assert _counts(engine, "monitor_reported_decision") == 0
    # 下一周期又可登记（无新候选则零删除，仍成功结案）。
    with make_session_factory(engine)() as fresh:
        later = _run_sweep(fresh, now=NOW + timedelta(seconds=SWEEP_INTERVAL + 1), engine=engine)
        assert later is not None
        assert later.completed
        fresh.commit()


def test_custom_station_group_uses_its_own_cutoff(
    committed_session: Session, engine: Engine
) -> None:
    station = _station_text()
    old = NOW - timedelta(days=200)
    _seed(committed_session, station, instance_id=11, received_at=old)
    committed_session.commit()
    overrides = PostgresStationRetentionOverrideRepository(committed_session)
    from dataclasses import replace

    short = replace(
        DEFAULT_RETENTION_POLICY,
        record_detail=RetentionRule(RetentionMode.DURATION, _DAY),
        record_compression_age=RetentionRule(RetentionMode.DURATION, _DAY),
    )
    overrides.insert_if_absent(
        StationRetentionRecord(
            station_id=UUID(station),
            mode=RetentionPolicyMode.CUSTOM,
            overrides=short,
            revision=1,
        )
    )
    committed_session.commit()
    result = _run_sweep(committed_session, engine=engine)
    assert result is not None
    assert result.completed
    committed_session.commit()
    assert _counts(engine, "monitor_reported_decision") == 0


def test_execution_failure_records_failure_and_next_cycle_reruns(
    committed_session: Session,
    engine: Engine,
) -> None:
    station = _station_text()
    old = NOW - timedelta(days=200)
    _seed(committed_session, station, instance_id=13, received_at=old)
    committed_session.commit()

    class ExplodingRetirement(RepositoryMonitorRetirement):
        def retire_records(self, *, cutoffs: object, limit: int, now: datetime) -> RecordRetirement:
            raise RuntimeError("injected failure")

    result = run_retention_sweep(
        policies=PostgresRetentionPolicyRepository(committed_session),
        records=PostgresStationRetentionOverrideRepository(committed_session),
        ledger=PostgresRetentionSweepRepository(committed_session),
        executor=ExplodingRetirement(
            session=committed_session, evidence_protected=lambda *_: False
        ),
        now=NOW,
        interval_seconds=SWEEP_INTERVAL,
    )
    assert result is not None
    assert not result.completed
    assert result.failure_code == "records:RuntimeError"
    committed_session.commit()
    assert _counts(engine, "monitor_reported_decision") == 1
    # 失败账本不阻塞下一周期重跑；原失败行保留可查询。
    with make_session_factory(engine)() as fresh:
        retried = _run_sweep(fresh, now=NOW + timedelta(seconds=SWEEP_INTERVAL + 1), engine=engine)
        assert retried is not None
        assert retried.completed
        fresh.commit()
    assert _counts(engine, "monitor_reported_decision") == 0


def test_longer_custom_group_is_excluded_from_global_delete(
    committed_session: Session,
    engine: Engine,
) -> None:
    from dataclasses import replace

    station = _station_text()
    _seed(committed_session, station, instance_id=21, received_at=NOW - timedelta(days=200))
    longer = replace(
        DEFAULT_RETENTION_POLICY, record_detail=RetentionRule(RetentionMode.DURATION, 365 * _DAY)
    )
    PostgresStationRetentionOverrideRepository(committed_session).insert_if_absent(
        StationRetentionRecord(UUID(station), RetentionPolicyMode.CUSTOM, longer, 1)
    )
    result = _run_sweep(committed_session, engine=engine)
    assert result is not None
    assert result.completed
    assert result.records is not None
    assert result.records.deleted == 0
    committed_session.commit()
    assert _counts(engine, "monitor_reported_decision") == 1


def test_bounded_cursor_advances_past_protected_oldest_records(
    committed_session: Session,
    engine: Engine,
) -> None:
    from factory_sop.retention.api import retention_cutoffs

    station = _station_text()
    _seed(
        committed_session,
        station,
        instance_id=31,
        received_at=NOW - timedelta(days=201),
        closed=False,
    )
    _seed(committed_session, station, instance_id=32, received_at=NOW - timedelta(days=200))
    cutoffs = retention_cutoffs(
        default=DEFAULT_RETENTION_POLICY, overrides=(), now=NOW, field="record_detail"
    )
    executor = _executor(committed_session)
    first = executor.retire_records(cutoffs=cutoffs, limit=1, now=NOW)
    second = executor.retire_records(cutoffs=cutoffs, limit=1, now=NOW)
    assert first.deleted == 0
    assert first.skips[0].count == 2
    assert second.deleted == 2
    committed_session.commit()
    assert _counts(engine, "monitor_reported_decision") == 1


def test_retired_identity_rejects_conflicting_replay_without_resurrection(
    committed_session: Session,
    engine: Engine,
) -> None:
    from dataclasses import replace

    from factory_sop.monitor.errors import MonitorRefusedError

    station = _station_text()
    old = NOW - timedelta(days=200)
    monitor = PostgresMonitorRepository(committed_session)
    monitor.upsert_instance(_instance(station, instance_id=41, received_at=old, closed=True))
    decision = _decision(station, instance_id=41, received_at=old)
    observation = _observation(station, instance_id=41, received_at=old)
    monitor.upsert_decision(decision)
    monitor.upsert_observation(observation)
    before = monitor.decision_sequence_for_event(decision.report.event_id)
    result = _run_sweep(committed_session, engine=engine)
    assert result is not None
    assert result.records is not None
    assert result.records.deleted == 2
    assert not monitor.upsert_decision(replace(decision, received_at=NOW))
    assert not monitor.upsert_observation(replace(observation, received_at=NOW))
    assert monitor.decision_sequence_for_event(decision.report.event_id) == before
    with pytest.raises(MonitorRefusedError):
        monitor.upsert_decision(
            replace(decision, report=replace(decision.report, trace_id="different"))
        )
    committed_session.commit()
    assert _counts(engine, "monitor_reported_decision") == 0
    assert _counts(engine, "monitor_observation") == 0


def test_partial_failure_keeps_success_counts_and_rolls_back_failed_owner_phase(
    committed_session: Session,
    engine: Engine,
) -> None:
    station = _station_text()
    _seed(committed_session, station, instance_id=51, received_at=NOW - timedelta(days=200))

    class FailingDisposals(RepositoryMonitorRetirement):
        def retire_disposals(self, **kwargs: object) -> DisposalRetirement:
            # 真正的数据库错误使 savepoint 失败；已完成明细阶段和账本仍须提交。
            committed_session.execute(text("SELECT 1 / 0"))
            raise AssertionError("unreachable")

    result = run_retention_sweep(
        policies=PostgresRetentionPolicyRepository(committed_session),
        records=PostgresStationRetentionOverrideRepository(committed_session),
        ledger=PostgresRetentionSweepRepository(committed_session),
        executor=FailingDisposals(session=committed_session, evidence_protected=lambda *_: False),
        now=NOW,
        interval_seconds=SWEEP_INTERVAL,
    )
    assert result is not None
    assert not result.completed
    assert result.records is not None
    assert result.records.deleted == 2
    assert result.disposals is None
    assert result.failure_code == "disposals:DataError"
    committed_session.commit()
    with engine.connect() as connection:
        row = connection.execute(text("SELECT status, result FROM retention_sweep")).one()
    assert row.status == "failed"
    assert row.result["records"]["deleted"] == 2
    assert row.result["disposals"] is None
