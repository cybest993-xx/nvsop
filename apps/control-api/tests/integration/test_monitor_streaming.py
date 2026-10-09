"""真实 PostgreSQL 验收：monitor SSE 提交顺序、短事务与提交后唤醒。"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from factory_sop.execution.adapters import dependencies as execution_dependencies
from factory_sop.monitor.adapters.repository import PostgresMonitorRepository
from factory_sop.monitor.adapters.streaming import PostgresMonitorStreamSource
from factory_sop.monitor.model import (
    MirroredDecision,
    MirroredHealth,
    MirroredObservation,
    MirroredSopInstance,
)
from nvsop_contracts import (
    ReportBackendProvenance,
    ReportedDecision,
    ReportedDisposal,
    ReportedHealth,
    ReportedObservation,
    ReportedSopInstance,
    ReportEvidence,
)

HOST_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f201")
SECOND_HOST_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f205")
STATION_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f202")
BACKEND_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f203")
RECEIVED_AT = datetime(2026, 9, 23, tzinfo=UTC)


def _clear_s143_monitor_rows(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM monitor_reported_decision WHERE host_id = :host_id"),
            {"host_id": str(HOST_ID)},
        )
        connection.execute(
            text("DELETE FROM monitor_reported_health WHERE host_id = :host_id"),
            {"host_id": str(HOST_ID)},
        )
        connection.execute(
            text("DELETE FROM monitor_observation WHERE host_id = :host_id"),
            {"host_id": str(HOST_ID)},
        )
        connection.execute(
            text("DELETE FROM monitor_sop_instance WHERE host_id IN (:host_id, :second_host_id)"),
            {"host_id": str(HOST_ID), "second_host_id": str(SECOND_HOST_ID)},
        )
        connection.execute(
            text("DELETE FROM monitor_disposal WHERE host_id = :host_id"),
            {"host_id": str(HOST_ID)},
        )


@pytest.fixture(autouse=True)
def clean_s143_monitor_rows(engine: Engine) -> Iterator[None]:
    _clear_s143_monitor_rows(engine)
    try:
        yield
    finally:
        _clear_s143_monitor_rows(engine)


def _decision(event_id: str) -> MirroredDecision:
    return MirroredDecision(
        report=ReportedDecision(
            event_id=event_id,
            trace_id=event_id,
            host_id=str(HOST_ID),
            station_id=str(STATION_ID),
            backend_id=str(BACKEND_ID),
            instance_id=1,
            verdict="indeterminate",
            reason_codes=("S143_TEST",),
            violations=(),
            lifecycle="closed",
            evidence=ReportEvidence(None, None, None),
            template_version_id=None,
            template_sha256=None,
            model_ids=("model-test",),
            reported_at="2026-09-23T00:00:00Z",
        ),
        received_at=RECEIVED_AT,
    )


def _health(event_id: str) -> MirroredHealth:
    return MirroredHealth(
        report=ReportedHealth(
            event_id=event_id,
            trace_id=event_id,
            host_id=str(HOST_ID),
            station_id=str(STATION_ID),
            stream_id="camera-main",
            status="available",
            reason_code="S143_TEST",
            detail="synthetic",
            occurred_at="2026-09-23T00:00:00Z",
            source_anchor=1.0,
            anchor_offset=0.5,
            reported_at="2026-09-23T00:00:00Z",
        ),
        received_at=RECEIVED_AT,
    )


def _insert(
    repository: PostgresMonitorRepository,
    kind: str,
    event_id: str,
) -> bool:
    if kind == "decision":
        return repository.upsert_decision(_decision(event_id))
    return repository.upsert_health(_health(event_id))


def _sequence(repository: PostgresMonitorRepository, kind: str, event_id: str) -> int:
    if kind == "decision":
        value = repository.decision_sequence_for_event(event_id)
    else:
        value = repository.health_sequence_for_event(event_id)
    assert value is not None
    return value


def _replay_ids(
    repository: PostgresMonitorRepository,
    kind: str,
    *,
    after_sequence: int,
) -> tuple[str, ...]:
    if kind == "decision":
        decisions = repository.decisions_after_sequence(after_sequence=after_sequence, limit=20)
        return tuple(value.report.event_id for value in decisions)
    health = repository.health_after_sequence(after_sequence=after_sequence, limit=20)
    return tuple(value.report.event_id for value in health)


def _wait_until_advisory_waiter(engine: Engine) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            waiting = connection.scalar(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted = false"
                )
            )
        if waiting:
            return
        time.sleep(0.02)
    pytest.fail("second monitor writer never waited on the stream advisory lock")


@pytest.mark.parametrize("kind", ["decision", "health"])
def test_stream_sequence_follows_commit_visibility_and_allows_rollback_gaps(
    engine: Engine,
    kind: str,
) -> None:
    factory = sessionmaker(bind=engine)
    first_id = f"s143:{kind}:first:{uuid4()}"
    second_id = f"s143:{kind}:second:{uuid4()}"
    rolled_id = f"s143:{kind}:rolled:{uuid4()}"
    after_gap_id = f"s143:{kind}:after-gap:{uuid4()}"
    second_started = threading.Event()
    second_committed = threading.Event()
    second_errors: list[BaseException] = []

    first_session = factory()
    try:
        first_repository = PostgresMonitorRepository(first_session)
        assert _insert(first_repository, kind, first_id)
        first_sequence = _sequence(first_repository, kind, first_id)

        def commit_second() -> None:
            try:
                with factory() as session:
                    second_started.set()
                    assert _insert(PostgresMonitorRepository(session), kind, second_id)
                    session.commit()
                second_committed.set()
            except BaseException as error:
                second_errors.append(error)

        worker = threading.Thread(target=commit_second)
        worker.start()
        assert second_started.wait(timeout=1.0)
        _wait_until_advisory_waiter(engine)
        assert not second_committed.is_set()

        first_session.commit()
        worker.join(timeout=2.0)
        assert not worker.is_alive()
        assert not second_errors
        assert second_committed.is_set()

        with factory() as session:
            repository = PostgresMonitorRepository(session)
            second_sequence = _sequence(repository, kind, second_id)
            assert second_sequence > first_sequence
            assert _replay_ids(repository, kind, after_sequence=first_sequence) == (second_id,)

        with factory() as session:
            repository = PostgresMonitorRepository(session)
            assert _insert(repository, kind, rolled_id)
            rolled_sequence = _sequence(repository, kind, rolled_id)
            session.rollback()

        with factory() as session:
            repository = PostgresMonitorRepository(session)
            assert _insert(repository, kind, after_gap_id)
            session.commit()

        with factory() as session:
            repository = PostgresMonitorRepository(session)
            after_gap_sequence = _sequence(repository, kind, after_gap_id)
            assert after_gap_sequence > rolled_sequence
            replayed = _replay_ids(repository, kind, after_sequence=second_sequence)
            assert replayed == (after_gap_id,)
    finally:
        first_session.close()


def _current_sequences(factory: sessionmaker[Session]) -> tuple[int, int]:
    with factory() as session:
        repository = PostgresMonitorRepository(session)
        decisions = repository.recent_decisions(limit=1)
        health = repository.recent_health(limit=1)
        return (
            (decisions[0].stream_sequence or 0) if decisions else 0,
            (health[0].stream_sequence or 0) if health else 0,
        )


def test_commit_wakes_independent_listeners_and_reads_use_short_transactions(
    engine: Engine,
) -> None:
    factory = sessionmaker(bind=engine)
    decision_sequence, health_sequence = _current_sequences(factory)
    source_a = PostgresMonitorStreamSource(
        factory,
        engine,
        execution_gateway_factory=execution_dependencies.grant_views,
    )
    source_b = PostgresMonitorStreamSource(
        factory,
        engine,
        execution_gateway_factory=execution_dependencies.grant_views,
    )
    event_ids = tuple(f"s143:wakeup:{uuid4()}" for _ in range(3))

    try:
        assert source_a.read_after_sequences(
            decision_sequence=decision_sequence,
            health_sequence=health_sequence,
            limit=20,
        ) == ((), ())
        assert source_b.read_after_sequences(
            decision_sequence=decision_sequence,
            health_sequence=health_sequence,
            limit=20,
        ) == ((), ())

        with engine.connect() as connection:
            idle_in_transaction = connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND state = 'idle in transaction' "
                    "AND pid <> pg_backend_pid()"
                )
            )
        assert idle_in_transaction == 0

        for event_id in event_ids:
            with factory() as session:
                assert PostgresMonitorRepository(session).upsert_health(_health(event_id))
                session.commit()
        time.sleep(0.05)

        assert source_a.wait_for_wakeup(timeout=1.0)
        assert source_b.wait_for_wakeup(timeout=1.0)
        assert not source_a.wait_for_wakeup(timeout=0)
        assert not source_b.wait_for_wakeup(timeout=0)

        _, health_a = source_a.read_after_sequences(
            decision_sequence=decision_sequence,
            health_sequence=health_sequence,
            limit=20,
        )
        _, health_b = source_b.read_after_sequences(
            decision_sequence=decision_sequence,
            health_sequence=health_sequence,
            limit=20,
        )
        assert set(event_ids) <= {value.report.event_id for value in health_a}
        assert set(event_ids) <= {value.report.event_id for value in health_b}
    finally:
        source_a.close()
        source_b.close()

    with engine.connect() as connection:
        listening = connection.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND query LIKE 'LISTEN nvsop_monitor_stream%'"
            )
        )
    assert listening == 0


def test_durable_replay_does_not_require_a_prior_wakeup(engine: Engine) -> None:
    factory = sessionmaker(bind=engine)
    decision_sequence, health_sequence = _current_sequences(factory)
    event_id = f"s143:missed-wakeup:{uuid4()}"

    with factory() as session:
        assert PostgresMonitorRepository(session).upsert_health(_health(event_id))
        session.commit()

    source = PostgresMonitorStreamSource(
        factory,
        engine,
        execution_gateway_factory=execution_dependencies.grant_views,
    )
    try:
        _, health = source.read_after_sequences(
            decision_sequence=decision_sequence,
            health_sequence=health_sequence,
            limit=20,
        )
        assert event_id in {value.report.event_id for value in health}
    finally:
        source.close()


def _instance(
    event_id: str,
    station_id: str,
    *,
    host_id: UUID = HOST_ID,
    instance_id: int = 1,
    received_at: datetime = RECEIVED_AT,
    closed: bool = False,
) -> MirroredSopInstance:
    report = ReportedSopInstance(
        event_id=event_id,
        trace_id=event_id,
        host_id=str(host_id),
        station_id=station_id,
        instance_id=instance_id,
        opened_at=1.0,
        closed_at=2.0 if closed else None,
        close_reason="closed_by_idle_timeout" if closed else None,
        open_boundary_signal="start",
        close_boundary_signal=None,
        template_version_id="actual-template",
        template_sha256="a" * 64,
        backend_provenance=(ReportBackendProvenance(str(BACKEND_ID), ("actual-model",)),),
        configuration_revision=1,
        configuration_sha256="b" * 64,
        reported_at="2026-09-23T00:00:00Z",
    )
    return MirroredSopInstance(report=report, received_at=received_at)


def _observation(event_id: str, station_id: str = str(STATION_ID)) -> MirroredObservation:
    return MirroredObservation(
        report=ReportedObservation(
            event_id=event_id,
            trace_id=event_id,
            host_id=str(HOST_ID),
            station_id=station_id,
            instance_id=1,
            source="action",
            signal="(1) synthetic action",
            source_time=1.0,
            source_anchor=10.0,
            observed_at=2.0,
            template_version_id="actual-template",
            template_sha256="a" * 64,
            backend=ReportBackendProvenance(str(BACKEND_ID), ("actual-model",)),
            reported_at="2026-09-23T00:00:00Z",
        ),
        received_at=RECEIVED_AT,
    )


def _station_instance(rows: tuple[dict[str, object], ...], station_id: str) -> dict[str, object]:
    return cast(
        dict[str, object],
        next(value["instance"] for value in rows if value["station_id"] == station_id),
    )


def test_runtime_projection_keeps_new_instance_when_old_close_arrives_late(engine: Engine) -> None:
    factory = sessionmaker(bind=engine)
    source = PostgresMonitorStreamSource(
        factory,
        engine,
        execution_gateway_factory=execution_dependencies.grant_views,
    )
    station_id, first, second = str(uuid4()), f"s019:instance:{uuid4()}", f"s019:instance:{uuid4()}"
    try:
        assert station_id not in {value["station_id"] for value in source.read_runtime_projection()}
        with factory() as session:
            repository = PostgresMonitorRepository(session)
            for event_id, instance_id in ((first, 1), (second, 2)):
                assert repository.upsert_instance(
                    _instance(
                        event_id,
                        station_id,
                        instance_id=instance_id,
                        received_at=RECEIVED_AT + timedelta(seconds=instance_id - 1),
                    ),
                )
            session.commit()
        assert source.wait_for_wakeup(timeout=1)
        assert _station_instance(source.read_runtime_projection(), station_id)["instance_id"] == 2

        with factory() as session:
            assert PostgresMonitorRepository(session).upsert_instance(
                _instance(
                    first, station_id, received_at=RECEIVED_AT + timedelta(seconds=2), closed=True
                ),
            )
            session.commit()
        assert source.wait_for_wakeup(timeout=1)
        latest = _station_instance(source.read_runtime_projection(), station_id)
        assert latest["instance_id"] == 2
        assert latest["closed_at"] is None
    finally:
        source.close()
    reconnect = PostgresMonitorStreamSource(
        factory,
        engine,
        execution_gateway_factory=execution_dependencies.grant_views,
    )
    try:
        assert _station_instance(reconnect.read_runtime_projection(), station_id) == latest
        with factory() as session:
            assert PostgresMonitorRepository(session).upsert_instance(
                _instance(
                    second,
                    station_id,
                    instance_id=2,
                    received_at=RECEIVED_AT + timedelta(seconds=3),
                    closed=True,
                )
            )
            session.commit()
        closed = _station_instance(reconnect.read_runtime_projection(), station_id)
        assert closed["instance_id"] == 2
        assert closed["close_reason"] == "closed_by_idle_timeout"
    finally:
        reconnect.close()


def test_runtime_projection_chooses_latest_host_before_host_local_instance_id(
    engine: Engine,
) -> None:
    factory = sessionmaker(bind=engine)
    station_id = str(uuid4())
    with factory() as session:
        repo = PostgresMonitorRepository(session)
        for host_id, instance_id, second in ((HOST_ID, 99, 0), (SECOND_HOST_ID, 1, 1)):
            assert repo.upsert_instance(
                _instance(
                    f"s019:host-choice:{uuid4()}",
                    station_id,
                    host_id=host_id,
                    instance_id=instance_id,
                    received_at=RECEIVED_AT + timedelta(seconds=second),
                )
            )
        session.commit()
    with factory() as session:
        latest = _station_instance(
            PostgresMonitorRepository(session).runtime_projection(), station_id
        )
    assert latest["host_id"] == str(SECOND_HOST_ID)
    assert latest["instance_id"] == 1


def test_runtime_observation_notify_is_transactional_and_projection_has_no_global_100_cap(
    engine: Engine,
) -> None:
    factory = sessionmaker(bind=engine)
    source = PostgresMonitorStreamSource(
        factory,
        engine,
        execution_gateway_factory=execution_dependencies.grant_views,
    )
    try:
        existing_stations = {value["station_id"] for value in source.read_runtime_projection()}
        rollback_station = str(uuid4())
        assert rollback_station not in existing_stations
        with factory() as session:
            assert PostgresMonitorRepository(session).upsert_observation(
                _observation(f"s019:rollback:{uuid4()}", rollback_station),
            )
            session.rollback()
        assert not source.wait_for_wakeup(timeout=0.05)

        station_ids = tuple(str(uuid4()) for _ in range(101))
        assert not set(station_ids) & existing_stations
        with factory() as session:
            repository = PostgresMonitorRepository(session)
            for station_id in station_ids:
                assert repository.upsert_observation(
                    _observation(f"s019:observation:{uuid4()}", station_id),
                )
            session.commit()
        assert source.wait_for_wakeup(timeout=1)
        projection = [
            value
            for value in source.read_runtime_projection()
            if value["station_id"] in station_ids
        ]
        assert len(projection) == 101
        assert {value["station_id"] for value in projection} == set(station_ids)
    finally:
        source.close()


def test_runtime_projection_health_uses_event_time_not_ingestion_sequence(engine: Engine) -> None:
    """#384: 迟到旧健康报告不得因更大的 Center 序号覆盖较新的业务事实。"""
    factory = sessionmaker(bind=engine)
    station_id, stream_id = str(uuid4()), f"s019:health:{uuid4()}"

    def sample(event_id: str, status: str, occurred_at: str) -> MirroredHealth:
        base = _health(event_id)
        return replace(
            base,
            report=replace(
                base.report,
                station_id=station_id,
                stream_id=stream_id,
                status=status,
                occurred_at=occurred_at,
                source_anchor=None,
                anchor_offset=None,
            ),
        )

    with factory() as session:
        assert PostgresMonitorRepository(session).upsert_health(
            sample(f"{stream_id}:newer", "source_error", "2026-09-23T00:01:00Z")
        )
        session.commit()
    # 事件时间更早但中心接收更晚：ingestion sequence 更大，旧实现会据此覆盖较新事实。
    with factory() as session:
        assert PostgresMonitorRepository(session).upsert_health(
            sample(f"{stream_id}:older-late", "delivering", "2026-09-23T00:00:00Z")
        )
        session.commit()
    with factory() as session:
        projection = PostgresMonitorRepository(session).runtime_projection()
    health = cast(
        list[dict[str, object]],
        next(value["health"] for value in projection if value["station_id"] == station_id),
    )
    assert [value["event_id"] for value in health] == [f"{stream_id}:newer"]
    assert health[0]["status"] == "source_error"


def test_disposal_mirror_is_idempotent_in_real_postgres(engine: Engine) -> None:
    event_id = f"s024:disposal:{uuid4()}"
    tail = ("key", "violation", "alert", "actor", "source", "result", None, 1.0, 1, "at")
    report = ReportedDisposal(event_id, str(HOST_ID), str(STATION_ID), 1, *tail)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        repository = PostgresMonitorRepository(session)
        assert repository.upsert_disposal(report, received_at=RECEIVED_AT) is True
        assert repository.upsert_disposal(report, received_at=RECEIVED_AT) is False
        session.commit()
        values, _ = repository.page_disposals(page=1, page_size=100)
    assert [value for value in values if value.event_id == event_id] == [report]


def test_health_mirror_is_idempotent_and_persists_stream_identity(engine: Engine) -> None:
    """AC1: 同一稳定事件身份重报只归档一次，且流身份随镜像行落库供看板定位。"""
    factory = sessionmaker(bind=engine)
    event_id = f"s018:health:{uuid4()}"

    with factory() as session:
        repository = PostgresMonitorRepository(session)
        assert repository.upsert_health(_health(event_id)) is True
        assert repository.upsert_health(_health(event_id)) is False
        session.commit()

    with engine.connect() as connection:
        rows = [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT stream_id, station_id FROM monitor_reported_health"
                    " WHERE event_id = :event_id"
                ),
                {"event_id": event_id},
            )
        ]
    assert rows == [("camera-main", str(STATION_ID))]
