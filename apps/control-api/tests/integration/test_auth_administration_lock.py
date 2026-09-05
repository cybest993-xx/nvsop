"""并发下的「最后管理员」守卫：两个事务不能都对着同一个前置状态通过检查。

守卫是先读后写，而两个并发事务都可能在对方提交前读到同一个前置状态——普通 `SELECT`
不带锁，两个「停用最后两名管理员」的操作就会都通过检查、都提交，留下一个无法通过产品
修复的系统。`acquire_administration_lock` 在读之前把这类操作串行化；这里的两个连接就是
那条竞态本身：A 拿锁写完不提交，B 必须等（lock_timeout 到点即失败），A 回滚后 B 重试才
能成功。
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from factory_sop.auth.adapters.repository import (
    PostgresRoleRepository,
    PostgresSessionRepository,
    PostgresUserRepository,
)
from factory_sop.auth.authorization import Caller
from factory_sop.auth.model import Role, User, UserStatus
from factory_sop.auth.passwords import hash_password
from factory_sop.auth.permissions import Permission
from factory_sop.auth.usecases.users import deactivate_user
from factory_sop.identifiers import new_id


def an_account(*, login_name: str) -> User:
    return User(
        id=new_id(),
        login_name=login_name,
        display_name=login_name,
        password_hash=hash_password("assembly-line-3"),
        status=UserStatus.ACTIVE,
    )


def a_caller(user: User) -> Caller:
    return Caller(user=user, granted=frozenset({Permission.USER_EDIT}))


def test_concurrent_deactivations_cannot_both_pass_the_guard(engine: Engine) -> None:
    setup = sessionmaker(bind=engine)()
    first = an_account(login_name="wang.li")
    second = an_account(login_name="zhao.min")
    PostgresUserRepository(setup).add(first)
    PostgresUserRepository(setup).add(second)
    administration = Role(
        id=new_id(),
        code="system_administrator",
        name="系统管理员",
        permissions=frozenset({Permission.USER_EDIT, Permission.ROLE_EDIT}),
    )
    roles_setup = PostgresRoleRepository(setup)
    roles_setup.add(administration)
    roles_setup.assign(user_id=first.id, role_ids=[administration.id])
    roles_setup.assign(user_id=second.id, role_ids=[administration.id])
    # Committed before the race: the guard's reads must see the same pre-state both sides saw.
    setup.commit()
    setup.close()

    # Transaction A takes the lock, passes the guard, writes, and holds the transaction open —
    # the write is not yet visible to anyone else.
    connection_a = engine.connect()
    transaction_a = connection_a.begin()
    session_a = sessionmaker(bind=connection_a)()
    outcome = deactivate_user(
        caller=a_caller(second),
        user_id=first.id,
        users=PostgresUserRepository(session_a),
        sessions=PostgresSessionRepository(session_a),
        roles=PostgresRoleRepository(session_a),
    )
    assert outcome.user.status is UserStatus.DEACTIVATED

    # Transaction B reads the pre-A state, takes the lock — and must block until A finishes.
    connection_b = engine.connect()
    transaction_b = connection_b.begin()
    connection_b.execute(text("SET LOCAL lock_timeout = '200ms'"))
    session_b = sessionmaker(bind=connection_b)()
    with pytest.raises(OperationalError):
        deactivate_user(
            caller=a_caller(first),
            user_id=second.id,
            users=PostgresUserRepository(session_b),
            sessions=PostgresSessionRepository(session_b),
            roles=PostgresRoleRepository(session_b),
        )
    transaction_b.rollback()
    session_b.close()
    connection_b.close()

    # A rolls back instead of committing — the lock is released either way, and B's retry now
    # sees A's outcome and succeeds.
    transaction_a.rollback()
    session_a.close()
    connection_a.close()

    retry = sessionmaker(bind=engine)()
    outcome = deactivate_user(
        caller=a_caller(first),
        user_id=second.id,
        users=PostgresUserRepository(retry),
        sessions=PostgresSessionRepository(retry),
        roles=PostgresRoleRepository(retry),
    )
    retry.commit()
    stored = PostgresUserRepository(retry).by_identifier(second.id)
    assert stored is not None
    assert stored.status is UserStatus.DEACTIVATED
    retry.close()
