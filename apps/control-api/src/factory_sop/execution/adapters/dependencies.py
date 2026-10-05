"""request-scoped UoW 中的 execution 真实适配器。"""

from factory_sop.execution.adapters.repository import (
    PostgresExecutionGrantRepository,
    PostgresHandoverRepository,
)
from factory_sop.execution.api import ExecutionLeaseGateway
from factory_sop.execution.repository import ExecutionGrantRepository, HandoverRepository
from factory_sop.execution.usecases import RepositoryExecutionLeaseGateway
from factory_sop.persistence import RequestSession


def grants(session: RequestSession) -> ExecutionGrantRepository:
    """返回参与当前请求事务的当前租约 owner seam。"""
    return PostgresExecutionGrantRepository(session)


def lease_gateway(session: RequestSession) -> ExecutionLeaseGateway:
    """返回参与当前请求事务的租约 owner seam；复用同一 grants 适配器。"""
    return RepositoryExecutionLeaseGateway(grants(session))


def handovers(session: RequestSession) -> HandoverRepository:
    """请求事务中的强制改绑确认记录。"""
    return PostgresHandoverRepository(session)


__all__ = ["grants", "handovers", "lease_gateway"]
