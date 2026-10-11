"""evidence 适配器绑定请求事务与 device 主机身份。"""

from factory_sop.device.api import DeviceHostGateway, DeviceMonitorGateway
from factory_sop.evidence.adapters.repository import (
    PostgresEvidenceRepository,
    PostgresEvidenceReviewRepository,
)
from factory_sop.evidence.repository import EvidenceRepository, EvidenceReviewRepository
from factory_sop.persistence import RequestSession


def evidence(session: RequestSession) -> EvidenceRepository:
    return PostgresEvidenceRepository(session)


def reviews(session: RequestSession) -> EvidenceReviewRepository:
    return PostgresEvidenceReviewRepository(session)


def host_gateway() -> DeviceHostGateway:
    """由控制面组合根绑定 device owner 的签名主机认证。"""
    raise RuntimeError("evidence host gateway dependency was not wired")


def registered_hosts() -> DeviceMonitorGateway:
    """签发仅认 device owner 当前登记的来源主机。"""
    raise RuntimeError("evidence host registry dependency was not wired")
