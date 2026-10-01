"""execution 用例访问当前租约的持久化 seam。"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from factory_sop.execution.model import HandoverConfirmation, StationGrant


class ExecutionGrantRepository(Protocol):
    """原子维护每个工位至多一条当前租约。"""

    def acquire_if_available(self, value: StationGrant) -> StationGrant | None:
        """无当前有效租约时建立候选租约；竞争失败返回 None。"""
        ...

    def renew_if_current(self, value: StationGrant) -> StationGrant | None:
        """仅续期仍有效且身份匹配的候选租约；失配或到期返回 None。"""
        ...

    def for_holder(self, host_id: UUID) -> tuple[StationGrant, ...]:
        """只返回该主机当前持有（含已过期）的租约，按工位排序。"""
        ...


class HandoverRepository(Protocol):
    """强制改绑确认记录的持久化 seam；内容建立后不可变。"""

    def add(self, value: HandoverConfirmation) -> None:
        """写入一条新请求，操作者首确认随记录一起落库。"""
        ...

    def by_identifier(self, handover_id: UUID) -> HandoverConfirmation | None:
        """按请求 id 读取，未找到返回 None。"""
        ...

    def confirm_second(self, value: HandoverConfirmation) -> HandoverConfirmation | None:
        """仅当仍未第二确认且内容完全一致时原子记录第二人确认，否则返回 None。

        条件更新是并发防线：同一请求的重复确认只能有一个赢家，内容不符不会确认旧内容。
        """
        ...


__all__ = ["ExecutionGrantRepository", "HandoverRepository"]
