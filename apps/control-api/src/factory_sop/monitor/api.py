"""上报入口和组合根使用的最小 monitor 接缝。"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class HostOwnershipGateway(Protocol):
    """接收主机观测所需的最小设备事实。"""

    def owns_station(self, *, host_id: UUID, station_id: UUID) -> bool: ...

    def owns_station_backend(
        self, *, host_id: UUID, station_id: UUID, backend_id: UUID
    ) -> bool: ...
