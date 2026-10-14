"""device 对 retention 的工位事实 seam：只提供身份、存在与启用，不读写保留覆盖。

retention 拥有工位覆盖记录；它只需要知道该工位是否存在、是否启用，因此这里只实现这一条只读事实，
不暴露、不读取也不写入任何保留配置。
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from factory_sop.device.model import DeviceStatus, InferenceHost
from factory_sop.device.repository import StationRepository
from factory_sop.retention.api import (
    EdgeImpactSource,
    EdgeImpactSourcesGateway,
    RetentionStationPresence,
    StationFactsGateway,
)


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


class RetentionSourceHosts(Protocol):
    def registered_host_ids(self) -> tuple[UUID, ...]: ...

    def by_id(self, host_id: UUID) -> InferenceHost | None: ...

    def has_historical_station(
        self,
        *,
        host_id: UUID,
        station_id: UUID,
        template_version_id: str | None = None,
        template_sha256: str | None = None,
    ) -> bool: ...


class RepositoryDeviceRetentionSources(EdgeImpactSourcesGateway):
    """保留源包含历史来源机,不因当前换机/停用而把旧主机的数据视作零。"""

    def __init__(self, *, hosts: RetentionSourceHosts) -> None:
        self._hosts = hosts

    def sources(self, *, station_id: UUID | None) -> tuple[EdgeImpactSource, ...]:
        values: list[EdgeImpactSource] = []
        for host_id in self._hosts.registered_host_ids():
            host = self._hosts.by_id(host_id)
            if (
                station_id is not None
                and host is not None
                and host.configuration_sha256 is not None
                and not self._hosts.has_historical_station(host_id=host_id, station_id=station_id)
            ):
                continue
            values.append(
                EdgeImpactSource(
                    host_id=host_id,
                    configuration_revision=None if host is None else host.configuration_revision,
                    configuration_sha256=None if host is None else host.configuration_sha256,
                )
            )
        return tuple(sorted(values, key=lambda item: str(item.host_id)))


__all__ = ["RepositoryDeviceRetentionSources", "RepositoryDeviceStationFacts"]
