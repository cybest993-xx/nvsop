"""device 对 retention 的工位事实 seam：只提供身份、存在与启用，不读写保留覆盖。

retention 拥有工位覆盖记录；它只需要知道该工位是否存在、是否启用，因此这里只实现这一条只读事实，
不暴露、不读取也不写入任何保留配置。
"""

from __future__ import annotations

from uuid import UUID

from factory_sop.device.model import DeviceStatus
from factory_sop.device.repository import StationRepository
from factory_sop.retention.api import RetentionStationPresence, StationFactsGateway


class RepositoryDeviceStationFacts(StationFactsGateway):
    """把工位存在/启用事实接到同一请求事务中的 `device_station`。"""

    def __init__(self, *, stations: StationRepository) -> None:
        self._stations = stations

    def station_presence(self, station_id: UUID) -> RetentionStationPresence:
        station = self._stations.by_id(station_id)
        if station is None:
            return RetentionStationPresence.MISSING
        if station.status is DeviceStatus.DEACTIVATED:
            return RetentionStationPresence.DEACTIVATED
        return RetentionStationPresence.ACTIVE


__all__ = ["RepositoryDeviceStationFacts"]
