"""execution 用例访问当前租约的持久化 seam。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from factory_sop.execution.model import HandoverConfirmation, NormalHandover, StationGrant


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

    def for_stations(self, station_ids: tuple[UUID, ...]) -> tuple[StationGrant, ...]:
        """无锁批量读取运行投影所需的当前租约事实。"""
        ...

    def for_station(self, station_id: UUID) -> StationGrant | None:
        """读取该工位当前租约，并在请求事务内锁定它；无归属返回 None。

        建立与第二确认都以它复核执行权关系，避免审批过程中租约被替换后仍确认旧关系。
        """
        ...


class NormalHandoverCommandGateway(Protocol):
    """经 device 命令 owner 登记正常交权停机命令。"""

    def enqueue(
        self,
        *,
        command_id: UUID,
        handover_id: UUID,
        host_id: UUID,
        requested_by: UUID,
        now: datetime,
    ) -> None:
        """与正常交权请求在同一请求事务中入队。"""
        ...


class NormalHandoverRepository(Protocol):
    """正常交权状态机的唯一 Center 持久化 seam。"""

    def add_or_get(self, value: NormalHandover) -> tuple[NormalHandover, bool]:
        """按幂等键原子建立请求；返回已存在记录时不重复创建命令。"""
        ...

    def by_identifier(self, handover_id: UUID) -> NormalHandover | None:
        """读取一条正常交权请求。"""
        ...

    def by_identifier_for_update(self, handover_id: UUID) -> NormalHandover | None:
        """锁定一条请求，供取消、超时和旧机确认串行化。"""
        ...

    def by_idempotency_key(self, key: str) -> NormalHandover | None:
        """按客户端幂等键读取冻结请求。"""
        ...

    def pending_for_station(self, station_id: UUID) -> NormalHandover | None:
        """返回工位当前唯一 pending 请求。"""
        ...

    def save(self, value: NormalHandover) -> NormalHandover:
        """持久化同一请求的状态转移。"""
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
