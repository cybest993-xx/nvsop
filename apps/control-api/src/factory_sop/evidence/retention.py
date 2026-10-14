"""evidence 对 retention 保留影响估算的只读 seam：只计数引用，不接触媒体字节（ADR-0012）。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from factory_sop.evidence.repository import EvidenceImpactRepository
from factory_sop.retention.api import (
    EvidenceImpactGateway,
    EvidenceInstanceUsage,
)


class RepositoryEvidenceImpact(EvidenceImpactGateway):
    """把中心证据引用计数接到 retention owner 定义的 seam；不读 device/monitor 表。"""

    def __init__(self, *, repository: EvidenceImpactRepository) -> None:
        self._repository = repository

    def evidence_usage(
        self,
        *,
        station_id: UUID | None,
        exposed_from: datetime | None,
        exposed_to: datetime,
    ) -> tuple[EvidenceInstanceUsage, ...]:
        return self._repository.evidence_usage(
            station_id=None if station_id is None else str(station_id),
            exposed_from=exposed_from,
            exposed_to=exposed_to,
        )


__all__ = ["RepositoryEvidenceImpact"]
