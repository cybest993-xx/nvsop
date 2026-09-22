"""execution 用例访问当前租约的持久化 seam。"""

from __future__ import annotations

from typing import Protocol

from factory_sop.execution.model import StationGrant


class ExecutionGrantRepository(Protocol):
    """原子维护每个工位至多一条当前租约。"""

    def acquire_if_available(self, value: StationGrant) -> StationGrant | None:
        """无当前有效租约时建立候选租约；竞争失败返回 None。"""
        ...

    def renew_if_current(self, value: StationGrant) -> StationGrant | None:
        """仅续期仍有效且身份匹配的候选租约；失配或到期返回 None。"""
        ...


__all__ = ["ExecutionGrantRepository"]
