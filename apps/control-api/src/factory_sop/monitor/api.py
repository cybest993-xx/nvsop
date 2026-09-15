"""上报入口和组合根使用的最小 monitor 接缝。"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from factory_sop.auth.api import Caller
from factory_sop.monitor.repository import MonitorRepository


class HostOwnershipGateway(Protocol):
    """接收主机观测所需的最小设备事实。"""

    def owns_station(self, *, host_id: UUID, station_id: UUID) -> bool: ...

    def owns_station_backend(
        self, *, host_id: UUID, station_id: UUID, backend_id: UUID
    ) -> bool: ...


def summary(*, caller: Caller, monitor: MonitorRepository) -> dict[str, object]:
    """返回 overview 使用的权限裁剪观测摘要。"""
    from factory_sop.monitor.usecases import summary as build_summary

    return build_summary(caller=caller, monitor=monitor)


__all__ = ["HostOwnershipGateway", "summary"]
