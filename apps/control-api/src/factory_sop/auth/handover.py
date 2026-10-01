"""`auth` 提供给 `execution` 强制改绑双人确认的窄授权 seam。

确认必须在“此刻”重读谁有权：`Caller` 是请求开始时的快照，撤权可以发生在两次确认之间。
本 Protocol 在既有 `auth.administration` 锁内重读，与所有会削减授权的路径共用同一把锁，
不新增第二把锁。它不是 `RoleRepository` 的再导出，也不暴露 auth 表。

公开出口是 `auth.api`：`execution` 的用例只经 `auth.api` 导入它，不直接导入本模块，也不经
`auth.adapters` 的真实实现。本模块只含标准库与 `auth` 纯域成员，不引入适配器、FastAPI 或
SQLAlchemy。
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class HandoverAuthority(Protocol):
    """在管理锁内回答当前哪些在用账户持有强制改绑权限。"""

    def lock_and_read_holders(self) -> frozenset[UUID]:
        """取管理锁并返回当前在用且持 `execution.handover.edit` 的账户 id。"""
        ...


__all__ = ["HandoverAuthority"]
