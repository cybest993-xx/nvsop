"""monitor 对 retention 保留影响估算的只读 seam：只计数镜像事实，不重判、不删数据。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from factory_sop.monitor.repository import MonitorImpactRepository
from factory_sop.retention.api import (
    InstanceRetentionFact,
    MonitorImpactGateway,
    StationUsage,
)


class RepositoryMonitorImpact(MonitorImpactGateway):
    """把 monitor 镜像事实接到 retention owner 定义的 seam；不读 evidence/retention 表。"""

    def __init__(self, *, repository: MonitorImpactRepository) -> None:
        self._repository = repository

    def instance_facts(
        self, *, instance_ids: tuple[tuple[UUID, UUID, int], ...]
    ) -> tuple[InstanceRetentionFact, ...]:
        return self._repository.instance_retention_facts(instance_ids=instance_ids)

    def record_usage(
        self,
        *,
        station_id: UUID | None,
        exposed_from: datetime | None,
        exposed_to: datetime,
    ) -> tuple[StationUsage, ...]:
        return self._repository.record_impact(
            station_id=None if station_id is None else str(station_id),
            exposed_from=exposed_from,
            exposed_to=exposed_to,
        )

    def disposal_usage(
        self,
        *,
        station_id: UUID | None,
        exposed_from: datetime | None,
        exposed_to: datetime,
    ) -> tuple[StationUsage, ...]:
        return self._repository.disposal_impact(
            station_id=None if station_id is None else str(station_id),
            exposed_from=exposed_from,
            exposed_to=exposed_to,
        )


__all__ = ["RepositoryMonitorImpact"]
