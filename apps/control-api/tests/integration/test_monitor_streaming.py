"""真实 PostgreSQL 验收：monitor SSE 提交顺序、短事务与提交后唤醒。"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from factory_sop.monitor.adapters.repository import PostgresMonitorRepository
from factory_sop.monitor.adapters.streaming import PostgresMonitorStreamSource
from factory_sop.monitor.model import MirroredDecision, MirroredHealth
from nvsop_contracts import ReportedDecision, ReportedHealth, ReportEvidence

HOST_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f201")
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
            status="available",
            reason_code="S143_TEST",
            detail="synthetic",
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
    source_a = PostgresMonitorStreamSource(factory, engine)
    source_b = PostgresMonitorStreamSource(factory, engine)
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

    source = PostgresMonitorStreamSource(factory, engine)
    try:
        _, health = source.read_after_sequences(
            decision_sequence=decision_sequence,
            health_sequence=health_sequence,
            limit=20,
        )
        assert event_id in {value.report.event_id for value in health}
    finally:
        source.close()
