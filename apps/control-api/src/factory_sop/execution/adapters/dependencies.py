"""request-scoped UoW 中的 execution 真实适配器。"""

from factory_sop.device.api import DeviceExecutionHandoverCommandGateway
from factory_sop.execution.adapters.repository import (
    PostgresExecutionGrantRepository,
    PostgresHandoverRepository,
    PostgresNormalHandoverRepository,
)
from factory_sop.execution.api import ExecutionGrantViewGateway, ExecutionLeaseGateway
from factory_sop.execution.repository import (
    ExecutionGrantRepository,
    HandoverRepository,
    NormalHandoverRepository,
)
from factory_sop.execution.usecases import (
    RepositoryExecutionGrantViewGateway,
    RepositoryExecutionLeaseGateway,
)
from factory_sop.persistence import RequestSession


def grants(session: RequestSession) -> ExecutionGrantRepository:
    """返回参与当前请求事务的当前租约 owner seam。"""
    return PostgresExecutionGrantRepository(session)


def grant_views(session: RequestSession) -> ExecutionGrantViewGateway:
    """返回无锁的 Center 执行权只读投影。"""
    return RepositoryExecutionGrantViewGateway(grants(session))


def lease_gateway(session: RequestSession) -> ExecutionLeaseGateway:
    """返回参与当前请求事务的租约 owner seam；复用同一 grants 适配器。"""
    return RepositoryExecutionLeaseGateway(grants(session))


def normal_handovers(session: RequestSession) -> NormalHandoverRepository:
    """请求事务中的正常交权状态机记录。"""
    return PostgresNormalHandoverRepository(session)


def normal_handover_commands(
    session: RequestSession,
) -> DeviceExecutionHandoverCommandGateway:
    """把 device 命令 owner 接入 execution 的同一请求事务（组合根在 create_app 接线）。"""
    raise RuntimeError("normal handover command dependency was not wired")


def handovers(session: RequestSession) -> HandoverRepository:
    """请求事务中的强制改绑确认记录。"""
    return PostgresHandoverRepository(session)


__all__ = ["grant_views", "grants", "handovers", "lease_gateway"]
