"""S020 / #198：monitor 三张镜像事实表的真实 TimescaleDB 拥有者迁移。

真实 TimescaleDB 上从 0046 存量库升级到 head：存量行/查询/全局身份保留，三张 hypertable 可查
且无压缩策略（原生压缩归 S021）；跨分区重复与错误流序号在真实 SQL 层被复合外键拒绝；降级还原
0046 普通表且不卸载预装扩展，再升级仍保留。training 隔离见 ``test_training_database_topology.py``，
SSE 提交顺序/并发复用 ``test_monitor_streaming.py``。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, Table, create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_monitor_streaming import _decision, _health, _observation

from factory_sop.monitor.adapters.repository import PostgresMonitorRepository
from factory_sop.monitor.adapters.tables import (
    ReportedDecisionRow,
    ReportedHealthRow,
    ReportedObservationRow,
)
from factory_sop.monitor.errors import MonitorRefusedError
from factory_sop.monitor.model import MirroredDecision

CONTROL_API = Path(__file__).resolve().parents[2]
TIMESCALE_VERSION = "2.22.1"
FACTS = ("monitor_reported_decision", "monitor_reported_health", "monitor_observation")
IDENTITIES = (
    "monitor_decision_identity",
    "monitor_health_identity",
    "monitor_observation_identity",
)
LEGACY_RECEIVED_AT = datetime(2026, 9, 24, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Legacy:
    ids: dict[str, tuple[str, ...]]
    sequences: dict[str, tuple[int, ...]]


@dataclass(frozen=True, slots=True)
class MigrationFixture:
    engine: Engine
    configuration: Config
    legacy: Legacy


def _at(offset_seconds: int) -> datetime:
    return LEGACY_RECEIVED_AT + timedelta(seconds=offset_seconds)


_LegacyRow = ReportedDecisionRow | ReportedHealthRow | ReportedObservationRow

# 三张事实各自的 0046 行构造器；窄的已知清单，不做通用框架。
_LEGACY_BUILDERS: dict[str, Callable[[str, int], _LegacyRow]] = {
    "decision": lambda e, i: ReportedDecisionRow.from_domain(
        replace(_decision(e), received_at=_at(i))
    ),
    "health": lambda e, i: ReportedHealthRow.from_domain(replace(_health(e), received_at=_at(i))),
    "observation": lambda e, i: ReportedObservationRow.from_domain(
        replace(_observation(e), received_at=_at(i))
    ),
}


def _decision_with(event_id: str, *, verdict: str) -> MirroredDecision:
    base = _decision(event_id)
    return replace(base, report=replace(base.report, verdict=verdict))


def _legacy_insert(connection: Connection, row: _LegacyRow) -> int:
    """把一条存量事实写进 0046 schema；序号由库的 Identity 生成并取回。"""
    table = cast(Table, row.__table__)
    values = {
        column.key: getattr(row, column.key)
        for column in table.columns
        if column.key != "stream_sequence"
    }
    statement = table.insert().values(**values)
    if "stream_sequence" in table.c:
        return int(connection.execute(statement.returning(table.c.stream_sequence)).scalar_one())
    connection.execute(statement)
    return 0


def _hypertables(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return set(
            connection.execute(
                text("SELECT hypertable_name FROM timescaledb_information.hypertables")
            ).scalars()
        )


@pytest.fixture
def migration(engine: Engine) -> Iterator[MigrationFixture]:
    """真实 TimescaleDB：每个测试独立数据库，从 0046 存量库升级到 head。"""
    installer = create_engine(
        engine.url._replace(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    name = f"nvsop_timescale_{uuid4().hex[:12]}"
    with installer.connect() as connection:
        connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}"')
        connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    upgraded = create_engine(engine.url._replace(database=name))
    configuration = Config(str(CONTROL_API / "alembic.ini"))
    configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
    configuration.set_main_option(
        "sqlalchemy.url", upgraded.url.render_as_string(hide_password=False)
    )
    command.upgrade(configuration, "0046")

    ids = {kind: tuple(f"s020:{kind}:{uuid4()}" for _ in range(2)) for kind in _LEGACY_BUILDERS}
    sequences: dict[str, tuple[int, ...]] = {}
    with upgraded.begin() as connection:
        for kind, build in _LEGACY_BUILDERS.items():
            sequences[kind] = tuple(
                _legacy_insert(connection, build(event_id, i))
                for i, event_id in enumerate(ids[kind])
            )

    command.upgrade(configuration, "head")
    try:
        yield MigrationFixture(
            engine=upgraded,
            configuration=configuration,
            legacy=Legacy(ids=ids, sequences=sequences),
        )
    finally:
        upgraded.dispose()
        with installer.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}"')
        installer.dispose()


def test_owner_migration_preserves_rows_identity_and_hypertables(
    migration: MigrationFixture,
) -> None:
    """AC1/AC2：存量行、查询与全局身份保留；三个真实 hypertable 与扩展版本可查。"""
    engine, legacy = migration.engine, migration.legacy
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
            )
            == TIMESCALE_VERSION
        )
        assert set(FACTS) <= _hypertables(engine)
        # 只调用 create_hypertable 转换既有表，不创建压缩策略（原生压缩归 S021）。
        assert tuple(
            connection.execute(
                text(
                    "SELECT (SELECT count(*) FROM timescaledb_information.compression_settings), "
                    "(SELECT count(*) FROM timescaledb_information.jobs "
                    "WHERE proc_name = 'policy_compression')"
                )
            ).one()
        ) == (0, 0)
        identity = {
            row.event_id: row.stream_sequence
            for row in connection.execute(
                text("SELECT event_id, stream_sequence FROM monitor_decision_identity")
            )
        }
    assert identity == dict(zip(legacy.ids["decision"], legacy.sequences["decision"], strict=True))

    # 迁移后的 repository 查询结果与存量一致，流序号原样保留。
    with Session(engine) as session:
        repository = PostgresMonitorRepository(session)
        decisions = {
            value.report.event_id: value for value in repository.recent_decisions(limit=10)
        }
        health = {value.report.event_id: value for value in repository.recent_health(limit=10)}
        observations, _ = repository.page_observations(page=1, page_size=100)
    assert (
        tuple(decisions[i].stream_sequence for i in legacy.ids["decision"])
        == legacy.sequences["decision"]
    )
    assert (
        tuple(health[i].stream_sequence for i in legacy.ids["health"]) == legacy.sequences["health"]
    )
    assert {value.report.event_id for value in observations} >= set(legacy.ids["observation"])

    # 序号继续：新事件取到比存量更大的流序号。
    fresh_id = f"s020:fresh:{uuid4()}"
    with Session(engine) as session:
        repository = PostgresMonitorRepository(session)
        assert repository.upsert_decision(replace(_decision(fresh_id), received_at=_at(86400)))
        session.commit()
        fresh_sequence = repository.decision_sequence_for_event(fresh_id)
    assert fresh_sequence is not None
    assert fresh_sequence > max(legacy.sequences["decision"])

    # 降级回 0046 后 schema 与数据恢复且预装扩展保留，再升级仍保留（实测回滚，不凭文件存在）。
    command.downgrade(migration.configuration, "0046")
    with engine.connect() as connection:
        assert not set(IDENTITIES) & set(
            connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM pg_extension WHERE extname = 'timescaledb'")
            )
            == 1
        )
        restored = connection.execute(
            text(
                "SELECT event_id, stream_sequence FROM monitor_reported_decision "
                "WHERE event_id = :event_id"
            ),
            {"event_id": legacy.ids["decision"][0]},
        ).one()
    assert tuple(restored) == (legacy.ids["decision"][0], legacy.sequences["decision"][0])

    command.upgrade(migration.configuration, "head")
    with engine.connect() as connection:
        assert set(FACTS) <= _hypertables(engine)
        assert (
            connection.scalar(
                text("SELECT count(*) FROM monitor_reported_decision WHERE event_id = :event_id"),
                {"event_id": legacy.ids["decision"][0]},
            )
            == 1
        )


def test_cross_partition_duplicate_and_wrong_sequence_are_refused(
    migration: MigrationFixture,
) -> None:
    """AC2：同一 event 不能跨分区重复；错误流序号与重复身份在真实 SQL 层被拒绝。"""
    engine, legacy = migration.engine, migration.legacy
    original = legacy.ids["decision"][0]
    fact_insert = (
        "INSERT INTO monitor_reported_decision "
        "(event_id, received_at, stream_sequence, trace_id, host_id, station_id, payload) "
        "VALUES (:event_id, :received_at, :sequence, 't', 'h', 's', '{}'::jsonb)"
    )
    with engine.begin() as connection, pytest.raises(IntegrityError):
        # 同一 event_id 落到另一个时间分区：复合外键没有匹配身份行，插入被拒。
        connection.execute(
            text(fact_insert),
            {"event_id": original, "received_at": _at(30 * 86400), "sequence": 9999},
        )

    # 身份行存在但事实序号写错：外键包含 stream_sequence，插入被拒（不能只靠 Python）。
    probe_id = f"s020:probe:{uuid4()}"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO monitor_decision_identity (event_id, received_at) "
                "VALUES (:event_id, :received_at)"
            ),
            {"event_id": probe_id, "received_at": LEGACY_RECEIVED_AT},
        )
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(
            text(fact_insert),
            {"event_id": probe_id, "received_at": LEGACY_RECEIVED_AT, "sequence": 9999},
        )
    # 身份表全局唯一 event_id：第二个身份行无法插入。
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(
            text(
                "INSERT INTO monitor_decision_identity (event_id, received_at) "
                "VALUES (:event_id, :received_at)"
            ),
            {"event_id": original, "received_at": _at(30 * 86400)},
        )

    # 不同内容重报被拒绝，相同内容仍按幂等返回 False，不新增行。
    with Session(engine) as session:
        repository = PostgresMonitorRepository(session)
        with pytest.raises(MonitorRefusedError):
            repository.upsert_decision(_decision_with(original, verdict="fail"))
        session.rollback()
        assert repository.upsert_decision(_decision(original)) is False
        session.commit()
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM monitor_reported_decision WHERE event_id = :event_id"),
                {"event_id": original},
            )
            == 1
        )


def test_late_historical_event_keeps_cursor_and_does_not_rewrite_history(
    migration: MigrationFixture,
) -> None:
    """AC2：迟到历史事件按提交可见序号入流，不覆盖较新事实，游标继续可重放。"""
    engine, legacy = migration.engine, migration.legacy
    decision_ids, decision_sequences = legacy.ids["decision"], legacy.sequences["decision"]
    after = max(decision_sequences)
    late_id = f"s020:late:{uuid4()}"
    with Session(engine) as session:
        repository = PostgresMonitorRepository(session)
        assert repository.upsert_decision(
            replace(_decision(late_id), received_at=LEGACY_RECEIVED_AT - timedelta(days=1))
        )
        session.commit()
        late_sequence = repository.decision_sequence_for_event(late_id)
        replayed = tuple(
            value.report.event_id
            for value in repository.decisions_after_sequence(after_sequence=after, limit=20)
        )
    assert late_sequence is not None
    assert late_sequence > after
    assert replayed == (late_id,)

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT event_id, stream_sequence, received_at FROM monitor_reported_decision "
                "WHERE event_id IN (:first, :second)"
            ),
            {"first": decision_ids[0], "second": decision_ids[1]},
        ).all()
    assert {row.event_id: row.stream_sequence for row in rows} == dict(
        zip(decision_ids, decision_sequences, strict=True)
    )
    assert {row.received_at for row in rows} == {LEGACY_RECEIVED_AT, _at(1)}
