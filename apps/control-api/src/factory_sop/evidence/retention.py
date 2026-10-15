"""evidence 对 retention 保留影响估算的只读 seam：只计数引用，不接触媒体字节（ADR-0012）。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from factory_sop.evidence.model import EvidenceImpactCount
from factory_sop.evidence.repository import EvidenceImpactRepository
from factory_sop.retention.api import (
    EvidenceImpactGateway,
    EvidenceInstanceUsage,
    EvidenceReviewUsage,
    ImpactAggregate,
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
        return tuple(
            EvidenceInstanceUsage(
                host_id=value.host_id,
                station_id=value.station_id,
                instance_id=value.instance_id,
                unlinked=_aggregate(value.unlinked),
                linked=_aggregate(value.linked),
            )
            for value in self._repository.evidence_usage(
                station_id=None if station_id is None else str(station_id),
                exposed_from=exposed_from,
                exposed_to=exposed_to,
            )
        )

    def review_usage(
        self,
        *,
        station_id: UUID | None,
        exposed_from: datetime | None,
        exposed_to: datetime,
    ) -> tuple[EvidenceReviewUsage, ...]:
        """把在审复核状态聚合接到 retention owner 定义的 seam；复核状态归 evidence 拥有。"""
        return tuple(
            EvidenceReviewUsage(
                host_id=value.host_id,
                station_id=value.station_id,
                instance_id=value.instance_id,
                known=value.known,
                active=value.active,
            )
            for value in self._repository.review_usage(
                station_id=None if station_id is None else str(station_id),
                exposed_from=exposed_from,
                exposed_to=exposed_to,
            )
        )


def _aggregate(value: EvidenceImpactCount) -> ImpactAggregate:
    return ImpactAggregate(
        objects=value.objects,
        known_bytes=value.known_bytes,
        unknown_size_objects=value.unknown_size_objects,
        earliest=value.earliest,
        latest=value.latest,
    )


__all__ = ["RepositoryEvidenceImpact"]
