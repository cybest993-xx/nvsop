"""evidence 适配器绑定请求事务与 device 主机身份。"""

from factory_sop.device.api import DeviceEvidenceGateway, DeviceHostGateway, DeviceMonitorGateway
from factory_sop.evidence.adapters.repository import (
    PostgresEvidenceDeletionClaimRepository,
    PostgresEvidenceImpactRepository,
    PostgresEvidenceReclipRequestRepository,
    PostgresEvidenceRepository,
    PostgresEvidenceReviewRepository,
)
from factory_sop.evidence.deletion import RepositoryEvidenceDeletionGateway
from factory_sop.evidence.repository import (
    EvidenceReclipRequestRepository,
    EvidenceRepository,
    EvidenceReviewRepository,
)
from factory_sop.evidence.retention import RepositoryEvidenceImpact
from factory_sop.persistence import RequestSession
from factory_sop.retention.api import EvidenceDeletionGateway, EvidenceImpactGateway


def evidence(session: RequestSession) -> EvidenceRepository:
    return PostgresEvidenceRepository(session)


def reviews(session: RequestSession) -> EvidenceReviewRepository:
    return PostgresEvidenceReviewRepository(session)


def reclip_requests(session: RequestSession) -> EvidenceReclipRequestRepository:
    return PostgresEvidenceReclipRequestRepository(session)


def reclip_gateway() -> DeviceEvidenceGateway:
    """由控制面组合根绑定 device owner 的来源主机窗口与委托命令 seam。"""
    raise RuntimeError("evidence reclip gateway dependency was not wired")


def host_gateway() -> DeviceHostGateway:
    """由控制面组合根绑定 device owner 的签名主机认证。"""
    raise RuntimeError("evidence host gateway dependency was not wired")


def registered_hosts() -> DeviceMonitorGateway:
    """签发仅认 device owner 当前登记的来源主机。"""
    raise RuntimeError("evidence host registry dependency was not wired")


def deletion_gateway(session: RequestSession) -> EvidenceDeletionGateway:
    """请求事务中的引用与保护事实、删除认领与反馈 seam，供 retention 判定使用。"""
    return RepositoryEvidenceDeletionGateway(
        evidence=PostgresEvidenceRepository(session),
        reviews=PostgresEvidenceReviewRepository(session),
        reclips=PostgresEvidenceReclipRequestRepository(session),
        claims=PostgresEvidenceDeletionClaimRepository(session),
    )


def retention_impact(session: RequestSession) -> EvidenceImpactGateway:
    """请求事务中的证据引用计数 seam，供 retention 影响估算使用。"""
    return RepositoryEvidenceImpact(repository=PostgresEvidenceImpactRepository(session))
