"""execution 拥有的当前物理执行权租约事实。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class StationGrant:
    """一个工位当前由中心确认的物理执行权租约。"""

    grant_id: UUID
    station_id: UUID
    holder_host_id: UUID
    lease_expires_at: datetime
    renewed_at: datetime
    request_id: UUID


__all__ = ["StationGrant"]
