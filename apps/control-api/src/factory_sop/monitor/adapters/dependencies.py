"""绑定到请求工作单元的 monitor HTTP 依赖。"""

from collections.abc import Iterator
from typing import cast

from fastapi import Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from factory_sop.device.api import DeviceHistoricalAssignmentGateway, DeviceHostGateway
from factory_sop.monitor.adapters.repository import PostgresMonitorRepository
from factory_sop.monitor.adapters.streaming import PostgresMonitorStreamSource
from factory_sop.monitor.repository import MonitorRepository, MonitorStreamSource
from factory_sop.persistence import RequestSession


def monitor(session: RequestSession) -> MonitorRepository:
    return PostgresMonitorRepository(session)


def host_gateway() -> DeviceHostGateway:
    """由 composition root 注入 device owner 的主机认证/归属 seam。"""
    raise RuntimeError("monitor host gateway dependency was not wired")


def historical_assignment_gateway() -> DeviceHistoricalAssignmentGateway:
    """由 composition root 注入 device owner 的历史 assignment seam。"""
    raise RuntimeError("monitor historical assignment dependency was not wired")


def monitor_stream_source(request: Request) -> Iterator[MonitorStreamSource]:
    """为 SSE 保留独立 listener；每轮事实读取由 source 自己创建并释放短 Session。"""
    factory = cast(sessionmaker[Session], request.app.state.session_factory)
    engine = cast(Engine, factory.kw["bind"])
    source = PostgresMonitorStreamSource(factory, engine)
    try:
        yield source
    finally:
        source.close()
