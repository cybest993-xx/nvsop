"""request-scoped UoW 中的 execution 真实适配器。"""

from factory_sop.execution.adapters.repository import (
    PostgresExecutionGrantRepository,
    PostgresHandoverRepository,
)
from factory_sop.execution.api import ExecutionLeaseGateway
from factory_sop.execution.repository import HandoverRepository
from factory_sop.execution.usecases import RepositoryExecutionLeaseGateway
from factory_sop.persistence import RequestSession


def lease_gateway(session: RequestSession) -> ExecutionLeaseGateway:
    """返回参与当前请求事务的租约 owner seam。"""
    return RepositoryExecutionLeaseGateway(PostgresExecutionGrantRepository(session))


def handovers(session: RequestSession) -> HandoverRepository:
    """请求事务中的强制改绑确认记录。"""
    return PostgresHandoverRepository(session)


__all__ = ["handovers", "lease_gateway"]
