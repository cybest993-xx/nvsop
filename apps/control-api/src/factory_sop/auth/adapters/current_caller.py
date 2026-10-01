"""worker 组合根使用的当前操作者解析器。

后台任务在派发时按持久化的操作者身份复核权限：这里只读当前账号与角色，不依赖浏览器
会话是否仍然存在。账号已删除或已停用时返回 `None`，由用例按拒绝处理。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from factory_sop.auth.adapters.repository import PostgresRoleRepository, PostgresUserRepository
from factory_sop.auth.authorization import Caller, CurrentCallerResolver
from factory_sop.auth.model import UserStatus


def current_caller_resolver(
    factory: sessionmaker[Session],
) -> CurrentCallerResolver:
    """返回按 `user_id` 读取当前权限快照的解析器。"""

    def resolve(user_id: UUID) -> Caller | None:
        with factory() as session:
            user = PostgresUserRepository(session).by_identifier(user_id)
            if user is None or user.status is UserStatus.DEACTIVATED:
                return None
            return Caller(
                user=user,
                granted=PostgresRoleRepository(session).permissions_of(user_id),
            )

    return resolve
