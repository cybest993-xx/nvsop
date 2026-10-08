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
