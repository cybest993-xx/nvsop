"""在一个请求级事务中组合权限范围摘要的 HTTP 适配器。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from factory_sop.auth.api import Authorized
from factory_sop.overview.api import (
    OverviewFailedError,
    OverviewSources,
    OverviewUnavailableError,
    build_overview,
)
from factory_sop.persistence import RequestSession

OverviewStatus = Literal[
    "available",
    "no_data",
    "not_permitted",
    "unavailable",
    "failed",
    "partial",
]


class ResourceSummary(BaseModel):
    """配置资源计数，并保留未知状态值。"""

    total: int = Field(ge=0)
    active: int = Field(ge=0)
    deactivated: int = Field(ge=0)
    unknown: int = Field(ge=0)
    by_status: dict[str, int] = Field(default_factory=dict)


class BackendSummary(ResourceSummary):
    """资源计数以及真实端点连接观测。"""

    connection_states: dict[str, int] = Field(default_factory=dict)
    verified: int = Field(ge=0)
    unverified: int = Field(ge=0)


class CameraSummary(ResourceSummary):
    """相机计数以及已持久化的凭据配置事实。"""

    credentials_configured: int = Field(ge=0)
    credentials_not_configured: int = Field(ge=0)


class ConnectorSummary(ResourceSummary):
    """连接器计数以及实测可达性，包含未验证状态。"""

    reachability: dict[str, int] = Field(default_factory=dict)
    verified: int = Field(ge=0)
    unverified: int = Field(ge=0)


class DeviceSummaryData(BaseModel):
    inference_hosts: ResourceSummary | None = None
    inference_backends: BackendSummary | None = None
    stations: ResourceSummary | None = None
    cameras: CameraSummary | None = None
    connectors: ConnectorSummary | None = None
    points: ResourceSummary | None = None


class TemplateImportSummary(BaseModel):
    total: int = Field(ge=0)
    by_status: dict[str, int] = Field(default_factory=dict)


class PublishedVersionSummary(BaseModel):
    total: int = Field(ge=0)
    sha256_verified: int = Field(ge=0)
    sha256_unverified: int = Field(ge=0)


class TemplateSummaryData(BaseModel):
    drafts: dict[str, int] | None = None
    imports: TemplateImportSummary | None = None
    published_versions: PublishedVersionSummary | None = None


class DatasetMemberSummary(BaseModel):
    total: int = Field(ge=0)
    by_status: dict[str, int] = Field(default_factory=dict)


class DatasetSummaryData(BaseModel):
    datasets: dict[str, int] | None = None
    members: DatasetMemberSummary | None = None


class MonitorSummaryData(BaseModel):
    recent_decisions: int | None = Field(default=None, ge=0)
    recent_health: int | None = Field(default=None, ge=0)
    runtime_status: str | None = None


class DeviceOverviewSection(BaseModel):
    # 用例保证状态属于 OverviewStatus；wire schema 保持旧版字符串兼容性。
    status: str
    data: DeviceSummaryData
    detail: str | None = None


class TemplateOverviewSection(BaseModel):
    # 用例保证状态属于 OverviewStatus；wire schema 保持旧版字符串兼容性。
    status: str
    data: TemplateSummaryData
    detail: str | None = None


class DatasetOverviewSection(BaseModel):
    # 用例保证状态属于 OverviewStatus；wire schema 保持旧版字符串兼容性。
    status: str
    data: DatasetSummaryData
    detail: str | None = None


class MonitorOverviewSection(BaseModel):
    # 用例保证状态属于 OverviewStatus；wire schema 保持旧版字符串兼容性。
    status: str
    data: MonitorSummaryData
    detail: str | None = None


class OverviewResponse(BaseModel):
    device: DeviceOverviewSection
    template: TemplateOverviewSection
    dataset: DatasetOverviewSection
    monitor: MonitorOverviewSection


def create_router(sources: OverviewSources) -> APIRouter:
    """用组合根提供的 owner source 创建 overview HTTP 路由。"""

    router = APIRouter(prefix="/overview", tags=["overview"])

    @router.get(
        "",
        operation_id="readOverview",
        response_model=OverviewResponse,
        response_model_exclude_none=True,
    )
    def read_overview(
        caller: Authorized,
        session: RequestSession,
    ) -> dict[str, object]:
        """使用同一个请求会话组合归属模块摘要; 调用方由各归属模块用例裁剪。"""

        return build_overview(
            device=lambda: _read_summary("device", lambda: sources.device(caller, session)),
            template=lambda: _read_summary("template", lambda: sources.template(caller, session)),
            dataset=lambda: _read_summary("dataset", lambda: sources.dataset(caller, session)),
            monitor=lambda: _read_summary("monitor", lambda: sources.monitor(caller, session)),
        )

    return router


def _read_summary(section: str, provider: Callable[[], dict[str, object]]) -> dict[str, object]:
    """将已知读取或分页失败转换为分区级部分失败。"""

    try:
        return provider()
    except SQLAlchemyError as error:
        raise OverviewUnavailableError(section) from error
    except ValueError as error:
        raise OverviewFailedError(f"{section} summary failed: {error}") from error


__all__ = ["OverviewResponse", "create_router"]
