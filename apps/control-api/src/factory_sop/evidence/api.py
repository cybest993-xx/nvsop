"""evidence 的跨模块接缝：只暴露别处需要的最小事实、类型与仓储 seam（ADR-0012 媒体归来源机）。"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from factory_sop.evidence.errors import EvidenceRefusal, EvidenceRefusedError
from factory_sop.evidence.model import (
    EvidenceKind,
    EvidenceOrigin,
    EvidenceReference,
    EvidenceRegistration,
    EvidenceStatus,
)
from factory_sop.evidence.repository import EvidenceRepository


class HostOwnershipGateway(Protocol):
    """登记用例所需的最小设备事实：来源主机是否拥有该工位。"""

    def owns_station(self, *, host_id: UUID, station_id: UUID) -> bool: ...


__all__ = [
    "EvidenceKind",
    "EvidenceOrigin",
    "EvidenceReference",
    "EvidenceRefusal",
    "EvidenceRefusedError",
    "EvidenceRegistration",
    "EvidenceRepository",
    "EvidenceStatus",
    "HostOwnershipGateway",
]
