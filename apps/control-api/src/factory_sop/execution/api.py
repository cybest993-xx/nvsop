"""其他中心模块可依赖的 execution 小型公共 interface。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from factory_sop.execution.errors import ExecutionRefusalCode, ExecutionRefusedError
from factory_sop.execution.model import StationGrant


class ExecutionLeaseGateway(Protocol):
    """供中心配置/交权组合层建立或续期工位当前物理执行权租约。"""

    def acquire(
        self,
        *,
        station_id: UUID,
        holder_host_id: UUID,
        request_id: UUID,
        now: datetime,
    ) -> StationGrant: ...

    def renew(
        self,
        *,
        station_id: UUID,
        grant_id: UUID,
        holder_host_id: UUID,
        request_id: UUID,
        now: datetime,
    ) -> StationGrant: ...


__all__ = [
    "ExecutionLeaseGateway",
    "ExecutionRefusalCode",
    "ExecutionRefusedError",
    "StationGrant",
]
