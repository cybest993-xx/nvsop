"""S020 / #198：monitor 三张镜像事实表的真实 TimescaleDB 拥有者迁移。

真实 TimescaleDB 上从 0046 存量库升级到 head：存量行/查询/全局身份保留，三张 hypertable 可查
并启用原生压缩；跨分区重复与错误流序号在真实 SQL 层被复合外键拒绝；降级还原 0046
普通表且不卸载预装扩展。training 隔离见 ``test_training_database_topology.py``，SSE 并发见
``test_monitor_streaming.py``。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, Table, create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from test_monitor_streaming import _decision, _health, _observation

from factory_sop.monitor.adapters.repository import PostgresMonitorRepository
from factory_sop.monitor.adapters.tables import (
    ReportedDecisionRow,
    ReportedHealthRow,
    ReportedObservationRow,
)
from factory_sop.monitor.errors import MonitorRefusedError
from factory_sop.retention.api import DEFAULT_RETENTION_POLICY

CONTROL_API = Path(__file__).resolve().parents[2]
TIMESCALE_VERSION = "2.22.1"
FACTS = ("monitor_reported_decision", "monitor_reported_health", "monitor_observation")
IDENTITIES = (
    "monitor_decision_identity",
    "monitor_health_identity",
    "monitor_observation_identity",
)
LEGACY_RECEIVED_AT = datetime(2026, 9, 24, tzinfo=UTC)
_LegacyRow = ReportedDecisionRow | ReportedHealthRow | ReportedObservationRow

# 原生 job advisory 的 locktag field4；TimescaleDB 2.22.1 用它避免与用户 advisory 锁冲突。
_NATIVE_JOB_ADVISORY_FIELD4 = 29749
_HEALTH_FACT = "monitor_reported_health"


def _health_compression_job_and_chunk(engine: Engine) -> tuple[int, str]:
    """取 health 原生压缩 job 与其已存在 chunk；两者都由 0051/0047 拥有。"""
    with engine.connect() as connection:
        job_id = connection.scalar(
            text(
                "SELECT job_id FROM timescaledb_information.jobs "
                "WHERE proc_name = 'policy_compression' AND hypertable_name = :fact"
            ),
            {"fact": _HEALTH_FACT},
        )
        chunk = connection.scalar(
            text(
                "SELECT chunk_schema || '.' || chunk_name FROM timescaledb_information.chunks "
                "WHERE hypertable_name = :fact ORDER BY range_start LIMIT 1"
            ),
            {"fact": _HEALTH_FACT},
        )
    assert job_id is not None, "health compression policy job missing"
    assert chunk is not None, "health chunk missing"
    return int(job_id), str(chunk)


def _health_native_advisory_holders(engine: Engine, job_id: int) -> list[int]:
    """pg_locks 中持该 job 原生 advisory 的 pid；field4 落在 objsubid 列。"""
    with engine.connect() as connection:
        return [
            int(pid)
            for pid in connection.execute(
                text(
                    "SELECT pid FROM pg_locks WHERE locktype = 'advisory' "
                    "AND classid = :job AND objid = 0 AND objsubid = :field4"
                ),
                {"job": job_id, "field4": _NATIVE_JOB_ADVISORY_FIELD4},
            ).scalars()
        ]


def _wait_for_blocked_health_worker(
    engine: Engine, job_id: int, chunk: str, *, timeout: float = 30.0
) -> None:
    """有界等待真实 scheduler 启动的 health worker 持 job advisory 且阻塞在该 chunk。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        holders = _health_native_advisory_holders(engine, job_id)
        if holders:
            with engine.connect() as connection:
                waiting = connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE NOT granted "
                        "AND pid = ANY(:pids) AND relation = CAST(:chunk AS regclass)"
                    ),
                    {"pids": holders, "chunk": chunk},
                )
            if waiting:
                return
        time.sleep(0.1)
    pytest.fail(
        "health native compression worker did not hold its job advisory while waiting on the chunk"
    )


def _dispose_failed_migration_engine(error: BaseException) -> None:
    """env.py 在迁移抛错时会跳过 engine.dispose()，其连接池连接不随 GC 关闭；
    从异常栈帧取回该 Engine 并显式释放，避免 teardown 的 DROP DATABASE 被它占用。"""
    traceback = error.__traceback__
    while traceback is not None:
        candidate = traceback.tb_frame.f_locals.get("engine")
        if isinstance(candidate, Engine):
            candidate.dispose()
        traceback = traceback.tb_next


def _wait_for_advisory_release(engine: Engine, job_id: int, *, timeout: float = 30.0) -> bool:
    """有界等待 worker 退出并释放 job advisory，避免 teardown 时仍有 session 占用数据库。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _health_native_advisory_holders(engine, job_id):
            return True
        time.sleep(0.1)
    return False


@dataclass(frozen=True, slots=True)
class MigrationFixture:
    engine: Engine
    configuration: Config
    ids: dict[str, tuple[str, ...]]
    sequences: dict[str, tuple[int, ...]]


def _at(offset_seconds: int) -> datetime:
    return LEGACY_RECEIVED_AT + timedelta(seconds=offset_seconds)


def _legacy_row(kind: str, event_id: str, offset: int) -> _LegacyRow:
    """在 0046 schema 中构造一条存量事实行；窄的已知三表清单，不做通用框架。"""
    if kind == "decision":
        return ReportedDecisionRow.from_domain(
            replace(_decision(event_id), received_at=_at(offset))
        )
    if kind == "health":
        return ReportedHealthRow.from_domain(replace(_health(event_id), received_at=_at(offset)))
    return ReportedObservationRow.from_domain(
        replace(_observation(event_id), received_at=_at(offset))
    )


def _legacy_insert(connection: Connection, row: _LegacyRow) -> int:
    """写进 0046 schema；序号由库的 Identity 生成并取回。"""
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


def _monitor_snapshot(engine: Engine) -> tuple[dict[str, tuple[tuple[object, ...], ...]], int]:
    """合成数据快照：三张事实表/身份表的全部行与生效的压缩年龄；失败降级与重放后须逐字一致。"""
    with engine.connect() as connection:
        rows = {
            table: tuple(
                tuple(row)
                for row in connection.execute(text(f"SELECT * FROM {table} ORDER BY event_id"))
            )
            for table in (*FACTS, *IDENTITIES)
        }
        stored_age = connection.scalar(
            text(
                "SELECT (policy -> 'record_compression_age' ->> 'seconds')::integer "
                "FROM retention_policy WHERE singleton = 1"
            )
        )
    age = (
        DEFAULT_RETENTION_POLICY.record_compression_age.seconds
        if stored_age is None
        else int(stored_age)
    )
    return rows, cast(int, age)


@pytest.fixture
def migration(engine: Engine) -> Iterator[MigrationFixture]:
    """真实 TimescaleDB：每个测试独立数据库，从 0046 存量库升级到 head。"""
    installer = create_engine(
        engine.url._replace(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    name = f"nvsop_timescale_{uuid4().hex[:12]}"
    with installer.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    upgraded = create_engine(engine.url._replace(database=name))
    configuration = Config(str(CONTROL_API / "alembic.ini"))
    configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
    configuration.set_main_option(
        "sqlalchemy.url", upgraded.url.render_as_string(hide_password=False)
    )
    command.upgrade(configuration, "0046")

    kinds = ("decision", "health", "observation")
    ids = {kind: tuple(f"s020:{kind}:{uuid4()}" for _ in range(2)) for kind in kinds}
    sequences: dict[str, tuple[int, ...]] = {}
    with upgraded.begin() as connection:
        for kind in kinds:
            sequences[kind] = tuple(
                _legacy_insert(connection, _legacy_row(kind, event_id, index))
                for index, event_id in enumerate(ids[kind])
            )
    command.upgrade(configuration, "head")
    try:
        yield MigrationFixture(
            engine=upgraded, configuration=configuration, ids=ids, sequences=sequences
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
    engine, ids, sequences = migration.engine, migration.ids, migration.sequences
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
            )
            == TIMESCALE_VERSION
        )
        assert set(FACTS) <= _hypertables(engine)
        assert tuple(
            connection.execute(
                text(
                    "SELECT (SELECT count(*) FROM timescaledb_information.compression_settings), "
                    "(SELECT count(*) FROM timescaledb_information.jobs "
                    "WHERE proc_name = 'policy_compression')"
                )
            ).one()
        ) == (3, 3)
        jobs = connection.execute(
            text(
                "SELECT hypertable_name, config FROM timescaledb_information.jobs "
                "WHERE proc_name = 'policy_compression'"
            )
        ).all()
        assert {job.hypertable_name for job in jobs} == set(FACTS)
        assert all(
            connection.scalar(
                text("SELECT CAST(:value AS interval)"),
                {"value": job.config["compress_after"]},
            ).total_seconds()
            == DEFAULT_RETENTION_POLICY.record_compression_age.seconds
            for job in jobs
        )
    with Session(engine) as session:
        repository = PostgresMonitorRepository(session)
        decisions = {
            value.report.event_id: value for value in repository.recent_decisions(limit=10)
        }
        health = {value.report.event_id: value for value in repository.recent_health(limit=10)}
        observations, _ = repository.page_observations(page=1, page_size=100)
        fresh_id = f"s020:fresh:{uuid4()}"
        assert repository.upsert_decision(replace(_decision(fresh_id), received_at=_at(86400)))
        session.commit()
        fresh_sequence = repository.decision_sequence_for_event(fresh_id)
    # 存量数据保持：读回的 synthetic report 与写入时逐字段一致，流序号原样保留。
    assert tuple(decisions[i].stream_sequence for i in ids["decision"]) == sequences["decision"]
    assert tuple(health[i].stream_sequence for i in ids["health"]) == sequences["health"]
    assert all(decisions[i].report == _decision(i).report for i in ids["decision"])
    assert all(health[i].report == _health(i).report for i in ids["health"])
    observations_by_id = {value.report.event_id: value.report for value in observations}
    assert all(observations_by_id[i] == _observation(i).report for i in ids["observation"])
    assert fresh_sequence is not None
    assert fresh_sequence > max(sequences["decision"])

    # 降级回 0046：还原普通表、数据保留且不卸载预装扩展；再升级仍保留（实测回滚，不凭文件存在）。
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
                "WHERE event_id = :id"
            ),
            {"id": ids["decision"][0]},
        ).one()
    assert tuple(restored) == (ids["decision"][0], sequences["decision"][0])
    command.upgrade(migration.configuration, "head")
    with engine.connect() as connection:
        assert set(FACTS) <= _hypertables(engine)
        assert (
            connection.scalar(
                text("SELECT count(*) FROM monitor_reported_decision WHERE event_id = :id"),
                {"id": ids["decision"][0]},
            )
            == 1
        )


def test_native_columnstore_preserves_reports_identity_and_sse_cursor(
    migration: MigrationFixture,
) -> None:
    """S021 AC2：真实 chunk 压缩后仍可读、幂等入库，SSE 序号持续且引用身份不被删除。"""
    engine, ids, sequences = migration.engine, migration.ids, migration.sequences
    with engine.begin() as connection:
        for fact in FACTS:
            chunk = connection.scalar(text(f"SELECT tableoid::regclass::text FROM {fact} LIMIT 1"))
            assert chunk is not None
            connection.execute(
                text("CALL convert_to_columnstore(CAST(:chunk AS regclass))"),
                {"chunk": chunk},
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM timescaledb_information.chunks "
                        "WHERE hypertable_name = :name AND is_compressed"
                    ),
                    {"name": fact},
                )
                >= 1
            )

    late_id = f"s021:late:{uuid4()}"
    with Session(engine) as session:
        repository = PostgresMonitorRepository(session)
        assert repository.upsert_decision(_decision(ids["decision"][0])) is False
        assert repository.upsert_health(_health(ids["health"][0])) is False
        assert repository.upsert_observation(_observation(ids["observation"][0])) is False
        assert {v.report.event_id for v in repository.recent_decisions(limit=20)} >= set(
            ids["decision"]
        )
        assert {v.report.event_id for v in repository.recent_health(limit=20)} >= set(ids["health"])
        observations, _ = repository.page_observations(page=1, page_size=20)
        assert {v.report.event_id for v in observations} >= set(ids["observation"])
        assert repository.upsert_decision(
            replace(_decision(late_id), received_at=LEGACY_RECEIVED_AT)
        )
        session.commit()
        new_seq = repository.decision_sequence_for_event(late_id)
        replay = tuple(
            item.report.event_id
            for item in repository.decisions_after_sequence(
                after_sequence=max(sequences["decision"]), limit=20
            )
        )

    assert new_seq is not None
    assert new_seq > max(sequences["decision"])
    assert replay == (late_id,)
    with engine.connect() as connection:
        for identity, kind in zip(IDENTITIES, ("decision", "health", "observation"), strict=True):
            present = set(connection.execute(text(f"SELECT event_id FROM {identity}")).scalars())
            assert present >= set(ids[kind])
        assert (
            connection.scalar(
                text("SELECT count(*) FROM monitor_reported_decision WHERE event_id = :id"),
                {"id": late_id},
            )
            == 1
        )

    # 已转换为 columnstore 的 chunk 在降级回 rowstore 后必须保留同一事件与序号。
    command.downgrade(migration.configuration, "0050")
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM monitor_reported_decision WHERE event_id = :id"),
                {"id": late_id},
            )
            == 1
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM monitor_decision_identity WHERE event_id = :id"),
                {"id": late_id},
            )
            == 1
        )
    command.upgrade(migration.configuration, "head")


def test_native_policies_use_existing_configured_age_on_upgrade(
    migration: MigrationFixture,
) -> None:
    """S021 AC1：历史库已保存的策略优先于默认值，升级后即用于三个原生作业。"""
    from factory_sop.retention.adapters.repository import PostgresRetentionPolicyRepository
    from factory_sop.retention.api import RetentionMode, RetentionPolicyState, RetentionRule

    engine, config = migration.engine, migration.configuration
    command.downgrade(config, "0050")
    selected = replace(
        DEFAULT_RETENTION_POLICY,
        record_compression_age=RetentionRule(RetentionMode.DURATION, 2 * 24 * 60 * 60),
    )
    with Session(engine) as session:
        assert PostgresRetentionPolicyRepository(session).replace_if_current(
            expected_revision=0, value=RetentionPolicyState(selected, 1)
        )
        session.commit()
    command.upgrade(config, "head")
    with engine.connect() as connection:
        jobs = connection.execute(
            text(
                "SELECT hypertable_name, CAST(config ->> 'compress_after' AS interval) AS age "
                "FROM timescaledb_information.jobs WHERE proc_name = 'policy_compression'"
            )
        ).all()
    assert {job.hypertable_name for job in jobs} == set(FACTS)
    assert all(job.age == timedelta(days=2) for job in jobs)


def test_downgrade_does_not_deadlock_against_running_native_compression(
    migration: MigrationFixture,
) -> None:
    """CI 37884171294：真实 scheduler 启动的 health worker 持 job advisory 等 chunk 时，降级删策略
    不得形成 advisory↔bgw_job_stat 死锁。旧实现同一事务连续 remove 会稳定复现该死锁。"""
    engine, config = migration.engine, migration.configuration
    job_id, chunk = _health_compression_job_and_chunk(engine)

    # 持 health chunk 的 ACCESS EXCLUSIVE，使真实 worker 在压缩该 chunk 前停住，
    # 并持 native job advisory。
    locker = create_engine(engine.url)
    lock_connection = locker.connect()
    lock_transaction = lock_connection.begin()
    lock_connection.execute(text(f"LOCK TABLE {chunk} IN ACCESS EXCLUSIVE MODE"))
    try:
        with engine.connect() as connection:
            connection = connection.execution_options(isolation_level="AUTOCOMMIT")
            connection.execute(text("SELECT alter_job(:job, next_start => now())"), {"job": job_id})
            # alter_job 只改 catalog；SIGHUP 唤醒 scheduler 重读 next_start 并立即启动真实 worker。
            connection.execute(text("SELECT pg_reload_conf()"))
        _wait_for_blocked_health_worker(engine, job_id, chunk)
        command.downgrade(config, "0050")
    finally:
        if lock_transaction.is_active:
            lock_transaction.rollback()
        lock_connection.close()
        locker.dispose()
        # best-effort 让被取消的 worker 收尾退出，不掩盖真正的失败（红例仍会抛 DeadlockDetected）。
        _wait_for_advisory_release(engine, job_id)

    assert _wait_for_advisory_release(engine, job_id), (
        "health worker did not release its job advisory"
    )
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM timescaledb_information.jobs WHERE job_id = :job"),
                {"job": job_id},
            )
            == 0
        )


def test_downgrade_replays_after_ddl_failure_with_partial_policy_removal(
    migration: MigrationFixture,
) -> None:
    """失败契约：删策略阶段非原子、DDL 阶段单事务。先经公开策略仓储写入非默认 2 天压缩年龄，再用真实
    SQL 依赖让 DDL 阶段的 DROP FUNCTION 失败：三策略删除已提交、revision 停 0051，事实行/身份行与
    该非默认年龄逐字保留；修复依赖后重复 downgrade 0050 成功，再 upgrade head 恢复该年龄。"""
    from factory_sop.retention.adapters.repository import PostgresRetentionPolicyRepository
    from factory_sop.retention.api import RetentionMode, RetentionPolicyState, RetentionRule

    engine, config = migration.engine, migration.configuration
    selected = replace(
        DEFAULT_RETENTION_POLICY,
        record_compression_age=RetentionRule(RetentionMode.DURATION, 2 * 24 * 60 * 60),
    )
    with Session(engine) as session:
        assert PostgresRetentionPolicyRepository(session).replace_if_current(
            expected_revision=0, value=RetentionPolicyState(selected, 1)
        )
        session.commit()
    before = _monitor_snapshot(engine)
    assert before[1] == selected.record_compression_age.seconds
    assert before[1] != DEFAULT_RETENTION_POLICY.record_compression_age.seconds
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE VIEW monitor_compression_age_probe AS "
                "SELECT monitor_set_compression_age(1) IS NULL AS age_probe"
            )
        )
    with pytest.raises(DBAPIError, match="depend") as caught:
        command.downgrade(config, "0050")
    _dispose_failed_migration_engine(caught.value)

    # 删策略阶段已提交，DDL 阶段回滚：revision 停 0051、三策略缺失，事实/identity/age 逐行保留。
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0051"
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM timescaledb_information.jobs "
                    "WHERE proc_name = 'policy_compression'"
                )
            )
            == 0
        )
    assert _monitor_snapshot(engine) == before

    # 修复依赖后重复 downgrade 0050：if_exists 跳过已删策略，DDL 成功，数据保留。
    with engine.begin() as connection:
        connection.execute(text("DROP VIEW monitor_compression_age_probe"))
    command.downgrade(config, "0050")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0050"
    assert _monitor_snapshot(engine) == before

    # 再 upgrade head：三策略恢复且沿用 configured age。
    command.upgrade(config, "head")
    with engine.connect() as connection:
        jobs = connection.execute(
            text(
                "SELECT hypertable_name, CAST(config ->> 'compress_after' AS interval) AS age "
                "FROM timescaledb_information.jobs WHERE proc_name = 'policy_compression'"
            )
        ).all()
    assert {job.hypertable_name for job in jobs} == set(FACTS)
    assert all(job.age.total_seconds() == before[1] for job in jobs)


def test_cross_partition_duplicate_and_wrong_sequence_are_refused(
    migration: MigrationFixture,
) -> None:
    """AC2：同一 event 不能跨分区重复；错误流序号与重复身份在真实 SQL 层被拒绝。"""
    engine, ids, sequences = migration.engine, migration.ids, migration.sequences
    original = ids["decision"][0]
    fact_insert = (
        "INSERT INTO monitor_reported_decision "
        "(event_id, received_at, stream_sequence, trace_id, host_id, station_id, payload) "
        "VALUES (:event_id, :received_at, :sequence, 't', 'h', 's', '{}'::jsonb)"
    )
    # 同一 event_id 落到另一分区、沿用正确序号：复合外键无匹配身份行。
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(
            text(fact_insert),
            {
                "event_id": original,
                "received_at": _at(30 * 86400),
                "sequence": sequences["decision"][0],
            },
        )
    # probe 已占用自己的全局序号；事实行借另一事件的序号被拒绝（不同事件不能共享序号）。
    probe_id = f"s020:probe:{uuid4()}"
    identity_insert = (
        "INSERT INTO monitor_decision_identity (event_id, received_at) "
        "VALUES (:event_id, :received_at)"
    )
    with engine.begin() as connection:
        connection.execute(
            text(identity_insert), {"event_id": probe_id, "received_at": LEGACY_RECEIVED_AT}
        )
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(
            text(fact_insert),
            {
                "event_id": probe_id,
                "received_at": LEGACY_RECEIVED_AT,
                "sequence": sequences["decision"][0],
            },
        )
    # 身份表全局唯一 event_id：第二个身份行无法插入。
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(
            text(identity_insert), {"event_id": original, "received_at": _at(30 * 86400)}
        )

    # 不同内容重报被拒绝，相同内容仍按幂等返回 False。
    conflicting = replace(
        _decision(original), report=replace(_decision(original).report, verdict="fail")
    )
    with Session(engine) as session:
        repository = PostgresMonitorRepository(session)
        with pytest.raises(MonitorRefusedError):
            repository.upsert_decision(conflicting)
        session.rollback()
        assert repository.upsert_decision(_decision(original)) is False
        session.commit()
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM monitor_reported_decision WHERE event_id = :id"),
                {"id": original},
            )
            == 1
        )


def test_late_historical_event_keeps_cursor_and_does_not_rewrite_history(
    migration: MigrationFixture,
) -> None:
    """AC2：迟到历史事件按提交可见序号入流，不覆盖较新事实，游标继续可重放。"""
    engine, ids, sequences = migration.engine, migration.ids, migration.sequences
    decision_ids, decision_sequences = ids["decision"], sequences["decision"]
    after = max(decision_sequences)
    late_id = f"s020:late:{uuid4()}"
    # 事件实际发生/上报时间很旧，中心直到存量之后才收到；接收时间后移，payload 反映旧事件。
    late_report = replace(_decision(late_id).report, reported_at="2026-09-01T00:00:00Z")
    late = replace(
        _decision(late_id), report=late_report, received_at=LEGACY_RECEIVED_AT + timedelta(days=1)
    )
    with Session(engine) as session:
        repository = PostgresMonitorRepository(session)
        assert repository.upsert_decision(late)
        session.commit()
        late_sequence = repository.decision_sequence_for_event(late_id)
        replayed = tuple(
            value.report.event_id
            for value in repository.decisions_after_sequence(after_sequence=after, limit=20)
        )
        stored = repository.recent_decisions(limit=10)
    assert late_sequence is not None
    assert late_sequence > after
    assert replayed == (late_id,)
    assert next(value.report for value in stored if value.report.event_id == late_id) == late_report

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
