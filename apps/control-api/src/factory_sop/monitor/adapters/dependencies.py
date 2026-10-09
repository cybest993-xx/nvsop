"""绑定到请求工作单元的 monitor HTTP 依赖。"""

from collections.abc import Callable, Iterator
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request
from sqlalchemy import Engine
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from factory_sop.device.api import (
    DeviceHistoricalAssignmentGateway,
    DeviceHostGateway,
    DeviceMonitorGateway,
)
from factory_sop.execution.api import ExecutionGrantViewGateway
from factory_sop.monitor.adapters.repository import PostgresMonitorRepository
from factory_sop.monitor.adapters.streaming import PostgresMonitorStreamSource
from factory_sop.monitor.repository import MonitorRepository, MonitorStreamSource
from factory_sop.monitor.usecases import HOST_SILENCE_THRESHOLD_SECONDS
from factory_sop.persistence import RequestSession


def monitor(session: RequestSession) -> MonitorRepository:
    return PostgresMonitorRepository(session)


def host_gateway() -> DeviceHostGateway:
    """由 composition root 注入 device owner 的主机认证/归属 seam。"""
    raise RuntimeError("monitor host gateway dependency was not wired")


def device_monitor_gateway() -> DeviceMonitorGateway:
    """由 composition root 注入 device owner 的历史归属与主机清单 seam。"""
    raise RuntimeError("monitor device gateway dependency was not wired")


def historical_assignment_gateway() -> DeviceHistoricalAssignmentGateway:
    """由 composition root 注入 device owner 的历史 assignment seam。"""
    raise RuntimeError("monitor historical assignment dependency was not wired")


def execution_view_factory() -> Callable[[Session], ExecutionGrantViewGateway]:
    """由 composition root 注入 execution owner 的无锁只读视图 factory。"""
    raise RuntimeError("monitor execution view dependency was not wired")


def monitor_stream_source(
    request: Request,
    execution_gateway_factory: Annotated[
        Callable[[Session], ExecutionGrantViewGateway], Depends(execution_view_factory)
    ],
    once: bool = False,
) -> Iterator[MonitorStreamSource]:
    """长订阅先取得独立 listener 配额；有限快照不占用长期监听资源。"""
    factory = cast(sessionmaker[Session], request.app.state.session_factory)
    engine = cast(Engine, request.app.state.monitor_listener_engine)
    source = PostgresMonitorStreamSource(
        factory,
        engine,
        execution_gateway_factory=execution_gateway_factory,
        stale_after_seconds=HOST_SILENCE_THRESHOLD_SECONDS,
    )
    try:
        if not once:
            try:
                source.open()
            except PoolTimeoutError:
                raise HTTPException(
                    status_code=503, detail="monitor subscription limit reached"
                ) from None
        yield source
    finally:
        source.close()
