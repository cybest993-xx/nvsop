"""按权限裁剪的概览 HTTP 组合适配器。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.exc import SQLAlchemyError

from factory_sop.auth.api import Authorized
from factory_sop.dataset.adapters import dependencies as dataset_dependencies
from factory_sop.device.adapters import dependencies as device_dependencies
from factory_sop.monitor.adapters import dependencies as monitor_dependencies
from factory_sop.monitor.repository import MonitorRepository
from factory_sop.overview.usecases import (
    OverviewSection,
    OverviewUnavailableError,
    build_overview,
    dataset_summary,
    device_summary,
    monitor_summary,
    template_summary,
)
from factory_sop.persistence import RequestSession
from factory_sop.template.adapters import dependencies as template_dependencies

router = APIRouter(prefix="/overview", tags=["overview"])


@router.get("", operation_id="readOverview")
def read_overview(
    caller: Authorized,
    session: RequestSession,
    monitor: Annotated[MonitorRepository, Depends(monitor_dependencies.monitor)],
) -> dict[str, object]:
    """在一个请求工作单元中组合各 owner 摘要。"""
    return build_overview(
        caller=caller,
        device=lambda: _read_summary(
            "device",
            lambda: device_summary(
                caller=caller,
                hosts=device_dependencies.hosts(session),
                backends=device_dependencies.backends(session),
                stations=device_dependencies.stations(session),
                cameras=device_dependencies.cameras(session),
                connectors=device_dependencies.connectors(session),
                points=device_dependencies.points(session),
            ),
        ),
        template=lambda: _read_summary(
            "template",
            lambda: template_summary(
                caller=caller,
                templates=template_dependencies.templates(session),
            ),
        ),
        dataset=lambda: _read_summary(
            "dataset",
            lambda: dataset_summary(
                caller=caller,
                datasets=dataset_dependencies.datasets(session),
            ),
        ),
        monitor=lambda: _read_summary(
            "monitor", lambda: monitor_summary(caller=caller, monitor=monitor)
        ),
    )


def _read_summary(section: str, provider: Callable[[], OverviewSection]) -> OverviewSection:
    """在 HTTP 适配器边界把数据库故障转换为脱敏的 owner 失败。"""
    try:
        return provider()
    except SQLAlchemyError as error:
        raise OverviewUnavailableError(section) from error


__all__ = ["router"]
