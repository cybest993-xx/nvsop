"""真实 PostgreSQL 验证 retention 全局策略表、CAS、权限种子与授权用例。"""

from __future__ import annotations

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from factory_sop.auth.authorization import Caller
from factory_sop.auth.model import User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.identifiers import new_id
from factory_sop.retention.adapters.repository import PostgresRetentionPolicyRepository
from factory_sop.retention.api import (
    DEFAULT_RETENTION_POLICY,
    RetentionPolicyState,
    get_policy,
    update_policy,
)


def _caller(*permissions: Permission) -> Caller:
    return Caller(
        user=User(
            new_id(),
            "retention-operator",
            "Retention Operator",
            "argon2-encoded",  # pragma: allowlist secret
            UserStatus.ACTIVE,
        ),
        granted=frozenset(permissions),
    )


def test_permissions_and_policy_table_are_seeded_on_real_postgres(engine: Engine) -> None:
    with engine.connect() as connection:
        codes = set(connection.execute(text("SELECT code FROM auth_permission")).scalars())
        columns = set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'retention_policy'"
                )
            ).scalars()
        )
    assert {"retention.policy.view", "retention.policy.edit"} <= codes
    # 录像滚动窗口/原始素材窗口不在此表；本表只存策略组与 CAS revision。
    assert columns == {"singleton", "policy", "revision"}


def test_policy_cas_and_authorized_usecase_round_trip(session: Session) -> None:
    repository = PostgresRetentionPolicyRepository(session)
    assert repository.get_global() is None
    first = RetentionPolicyState(DEFAULT_RETENTION_POLICY, 1)
    assert repository.replace_if_current(expected_revision=0, value=first) is True
    assert repository.replace_if_current(expected_revision=0, value=first) is False
    assert repository.get_global() == first
    created = update_policy(
        _caller(Permission.RETENTION_POLICY_EDIT),
        DEFAULT_RETENTION_POLICY,
        expected_revision=1,
        repository=repository,
    )
    assert created.revision == 2
    assert get_policy(_caller(Permission.RETENTION_POLICY_VIEW), repository) == created
    # 更新策略不删除任何拥有者数据：策略表仍只有一行。
    assert session.execute(text("SELECT count(*) FROM retention_policy")).scalar_one() == 1


def test_record_compression_age_tracks_authorized_policy_and_refuses_native_failure(
    session: Session,
) -> None:
    """S021 AC1/AC3：持久策略同事务改变原生作业年龄；作业缺失时不误报写入成功。"""
    from dataclasses import replace
    from datetime import timedelta

    import pytest
    from sqlalchemy.exc import DBAPIError

    from factory_sop.retention.api import RetentionMode, RetentionRule

    repo = PostgresRetentionPolicyRepository(session)
    selected = replace(
        DEFAULT_RETENTION_POLICY,
        record_compression_age=RetentionRule(RetentionMode.DURATION, 2 * 24 * 60 * 60),
    )
    # 与生产 runtime role 一样，只授予 retention 表 DML，不给予 Timescale 作业管理权。
    session.execute(text("CREATE ROLE s021_runtime_policy_test NOLOGIN"))
    session.execute(text("GRANT USAGE ON SCHEMA public TO s021_runtime_policy_test"))
    session.execute(
        text("GRANT SELECT, INSERT, UPDATE ON retention_policy TO s021_runtime_policy_test")
    )
    session.execute(text("SET LOCAL ROLE s021_runtime_policy_test"))
    state = update_policy(
        _caller(Permission.RETENTION_POLICY_EDIT),
        selected,
        expected_revision=0,
        repository=repo,
    )
    assert state.revision == 1
    session.execute(text("RESET ROLE"))
    jobs = session.execute(
        text(
            "SELECT hypertable_name, CAST(config ->> 'compress_after' AS interval) AS age "
            "FROM timescaledb_information.jobs WHERE proc_name = 'policy_compression'"
        )
    ).all()
    assert {job.hypertable_name for job in jobs} == {
        "monitor_reported_decision",
        "monitor_reported_health",
        "monitor_observation",
    }
    assert all(job.age == timedelta(days=2) for job in jobs)
    assert (
        session.execute(text("SELECT count(*) FROM timescaledb_information.job_stats")).scalar_one()
        >= 3
    )

    savepoint = session.begin_nested()
    session.execute(text("CALL remove_columnstore_policy('monitor_observation')"))
    with pytest.raises(DBAPIError, match="expected 3 monitor native compression policies"):
        update_policy(
            _caller(Permission.RETENTION_POLICY_EDIT),
            DEFAULT_RETENTION_POLICY,
            expected_revision=1,
            repository=repo,
        )
    savepoint.rollback()
    assert repo.get_global() == state
    assert (
        session.execute(
            text(
                "SELECT count(*) FROM timescaledb_information.jobs "
                "WHERE proc_name = 'policy_compression'"
            )
        ).scalar_one()
        == 3
    )
