"""上报入口和组合根使用的最小 monitor 接缝。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from factory_sop.auth.api import Caller
from factory_sop.monitor.repository import MonitorRepository


class HostOwnershipGateway(Protocol):
    """接收主机观测所需的最小设备事实。"""

    def owns_station(self, *, host_id: UUID, station_id: UUID) -> bool:
        """判断认证主机是否拥有该工位。"""
        ...

    def owns_station_backend(self, *, host_id: UUID, station_id: UUID, backend_id: UUID) -> bool:
        """判断认证主机是否拥有该工位及推理后端。"""
        ...


@dataclass(frozen=True, slots=True)
class MonitorHostSources:
    """监控上报入口所需的主机认证和归属接口。"""

    authenticate: Callable[..., None]
    gateway: Callable[..., HostOwnershipGateway]


def summary(*, caller: Caller, monitor: MonitorRepository) -> dict[str, object]:
    """返回概览使用的观测摘要; 调用方权限由 monitor 用例负责裁剪。"""
    from factory_sop.monitor.usecases import summary as build_summary

    return build_summary(caller=caller, monitor=monitor)


__all__ = ["HostOwnershipGateway", "MonitorHostSources", "summary"]
