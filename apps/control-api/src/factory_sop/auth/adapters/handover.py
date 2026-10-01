"""强制改绑授权 seam 的真实实现：复用 `RoleRepository` 的管理锁与在用持有人查询。"""

from __future__ import annotations

from uuid import UUID

from factory_sop.auth.handover import HandoverAuthority
from factory_sop.auth.permissions import Permission
from factory_sop.auth.repository import RoleRepository


class PostgresHandoverAuthority(HandoverAuthority):
    """在请求事务内先取管理锁，再读当前持有人。"""

    def __init__(self, roles: RoleRepository) -> None:
        self._roles = roles

    def lock_and_read_holders(self) -> frozenset[UUID]:
        self._roles.acquire_administration_lock()
        return self._roles.active_holders_of(Permission.HANDOVER_EDIT)


__all__ = ["PostgresHandoverAuthority"]
