"""evidence 适配器绑定请求事务与 device 主机身份。"""

from factory_sop.device.api import DeviceHostGateway
from factory_sop.evidence.adapters.repository import PostgresEvidenceRepository
from factory_sop.evidence.repository import EvidenceRepository
from factory_sop.persistence import RequestSession


def evidence(session: RequestSession) -> EvidenceRepository:
    return PostgresEvidenceRepository(session)


def host_gateway() -> DeviceHostGateway:
    """由控制面组合根绑定 device owner 的签名主机认证。"""
    raise RuntimeError("evidence host gateway dependency was not wired")
