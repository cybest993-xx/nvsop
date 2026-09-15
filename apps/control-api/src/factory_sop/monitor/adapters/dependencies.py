"""绑定到请求工作单元的 monitor HTTP 依赖。"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from factory_sop.monitor.adapters.repository import PostgresMonitorRepository
from factory_sop.monitor.repository import MonitorRepository
from factory_sop.persistence import RequestSession, request_session


def monitor(session: RequestSession) -> MonitorRepository:
    return PostgresMonitorRepository(session)


StreamingSession = Annotated[Session, Depends(request_session, scope="request")]


def streaming_monitor(session: StreamingSession) -> MonitorRepository:
    """为流式响应保留到响应结束的只读 monitor session。"""
    return PostgresMonitorRepository(session)
