"""绑定到请求工作单元的 monitor HTTP 依赖。"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from factory_sop.device.api import DeviceHistoricalAssignmentGateway, DeviceHostGateway
from factory_sop.monitor.adapters.repository import PostgresMonitorRepository
from factory_sop.monitor.repository import MonitorRepository
from factory_sop.persistence import RequestSession, request_session


def monitor(session: RequestSession) -> MonitorRepository:
    return PostgresMonitorRepository(session)


def host_gateway() -> DeviceHostGateway:
    """由 composition root 注入 device owner 的主机认证/归属 seam。"""
    raise RuntimeError("monitor host gateway dependency was not wired")


def historical_assignment_gateway() -> DeviceHistoricalAssignmentGateway:
    """由 composition root 注入 device owner 的历史 assignment seam。"""
    raise RuntimeError("monitor historical assignment dependency was not wired")


StreamingSession = Annotated[Session, Depends(request_session, scope="request")]


def streaming_monitor(session: StreamingSession) -> MonitorRepository:
    """为流式响应保留到响应结束的只读 monitor session。"""
    return PostgresMonitorRepository(session)
