"""其他中心模块可依赖的 execution 小型公共 interface。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol
from uuid import UUID

from factory_sop.execution.errors import ExecutionRefusalCode, ExecutionRefusedError
from factory_sop.execution.model import StationGrant


class ExecutionGrantState(Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class ExecutionGrantView:
    station_id: UUID
    state: ExecutionGrantState
    grant: StationGrant | None


class ExecutionGrantViewGateway(Protocol):
    """供只读运行投影批量读取 Center 当前物理执行权事实。"""

    def station_views(
        self, *, station_ids: tuple[UUID, ...], now: datetime
    ) -> tuple[ExecutionGrantView, ...]: ...


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

    def renew_host_leases(
        self,
        *,
        host_id: UUID,
        now: datetime,
        request_id: UUID,
    ) -> tuple[StationGrant, ...]:
        """在主机成功拉取配置的边界续期其持有且仍可续期的租约。"""
        ...


__all__ = [
    "ExecutionGrantState",
    "ExecutionGrantView",
    "ExecutionGrantViewGateway",
    "ExecutionLeaseGateway",
    "ExecutionRefusalCode",
    "ExecutionRefusedError",
    "StationGrant",
]
