"""概览 HTTP 组合适配器；不拥有业务状态或产品用例。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Protocol

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from factory_sop.auth.api import Authorized, Caller
from factory_sop.observability import get_logger
from factory_sop.persistence import RequestSession

_logger = get_logger("overview")


class OverviewUnavailableError(RuntimeError):
    """某个所有者摘要无法为本次请求生成安全快照。"""


@dataclass(frozen=True, slots=True)
class OverviewSection:
    """HTTP 组合接缝使用的内部分段封装。"""

    status: str
    data: Mapping[str, object]
    detail: str | None = None

    def to_wire(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value is not None}


class SummaryReader(Protocol):
    """组合根提供的真实 owner 摘要读取器。"""

    def __call__(self, caller: Caller, session: RequestSession) -> dict[str, object]:
        """在同一请求事务中读取一个权限裁剪摘要。"""
        ...


@dataclass(frozen=True, slots=True)
class OverviewSources:
    """HTTP 组合适配器读取四个 owner 摘要的 source。"""

    device: SummaryReader
    template: SummaryReader
    dataset: SummaryReader
    monitor: SummaryReader


class ResourceSummary(BaseModel):
    """配置资源的数量，并保留未知状态值。"""

    total: int = Field(ge=0)
    active: int = Field(ge=0)
    deactivated: int = Field(ge=0)
    unknown: int = Field(ge=0)
    by_status: dict[str, int] = Field(default_factory=dict)


class BackendSummary(ResourceSummary):
    """资源数量及真实端点连接观测。"""

    connection_states: dict[str, int] = Field(default_factory=dict)
    verified: int = Field(ge=0)
    unverified: int = Field(ge=0)


class CameraSummary(ResourceSummary):
    """相机数量及已持久化的凭据存在标志。"""

    credentials_configured: int = Field(ge=0)
    credentials_not_configured: int = Field(ge=0)


class ConnectorSummary(ResourceSummary):
    """连接器数量及实测可达性，包括未验证状态。"""

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
    status: str
    data: DeviceSummaryData
    detail: str | None = None


class TemplateOverviewSection(BaseModel):
    status: str
    data: TemplateSummaryData
    detail: str | None = None


class DatasetOverviewSection(BaseModel):
    status: str
    data: DatasetSummaryData
    detail: str | None = None


class MonitorOverviewSection(BaseModel):
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
        """通过请求唯一的 SQLAlchemy session 组合所有者摘要。"""
        return compose_overview(
            device=lambda: _read_summary("device", lambda: sources.device(caller, session)),
            template=lambda: _read_summary("template", lambda: sources.template(caller, session)),
            dataset=lambda: _read_summary("dataset", lambda: sources.dataset(caller, session)),
            monitor=lambda: _read_summary("monitor", lambda: sources.monitor(caller, session)),
        )

    return router


def compose_overview(
    *,
    device: Callable[[], OverviewSection | Mapping[str, object]],
    template: Callable[[], OverviewSection | Mapping[str, object]],
    dataset: Callable[[], OverviewSection | Mapping[str, object]],
    monitor: Callable[[], OverviewSection | Mapping[str, object]],
) -> dict[str, object]:
    """组合 owner 摘要；单个读取失败不遮蔽其他真实结果。"""
    providers = {
        "device": device,
        "template": template,
        "dataset": dataset,
        "monitor": monitor,
    }
    unavailable_detail = {
        "device": "设备摘要暂时不可用",
        "template": "模板摘要暂时不可用",
        "dataset": "训练数据摘要暂时不可用",
        "monitor": "运行观测摘要暂时不可用",
    }
    result: dict[str, object] = {}
    failed: list[str] = []
    for key, provider in providers.items():
        try:
            result[key] = _section(provider()).to_wire()
        except OverviewUnavailableError as error:
            failed.append(key)
            _logger.warning(
                "overview.section.unavailable",
                section=key,
                error_type=type(error).__name__,
            )

    if failed:
        failure_status = "partial" if len(failed) < len(providers) else "unavailable"
        for key in failed:
            detail = unavailable_detail[key]
            if failure_status == "partial":
                detail = f"{detail}；其他模块仍返回真实摘要"
            result[key] = OverviewSection(
                status=failure_status,
                data={},
                detail=detail,
            ).to_wire()
    return result


def _read_summary(section: str, provider: Callable[[], dict[str, object]]) -> dict[str, object]:
    """将已知的读取或分页失败转换为分段级部分失败。"""
    try:
        return provider()
    except (SQLAlchemyError, ValueError) as error:
        raise OverviewUnavailableError(section) from error


def _section(value: OverviewSection | Mapping[str, object]) -> OverviewSection:
    if isinstance(value, OverviewSection):
        return value
    status = value.get("status")
    data = value.get("data")
    detail = value.get("detail")
    if not isinstance(status, str) or not isinstance(data, Mapping):
        raise OverviewUnavailableError("owner summary has an invalid envelope")
    if detail is not None and not isinstance(detail, str):
        raise OverviewUnavailableError("owner summary has an invalid detail")
    return OverviewSection(status=status, data=data, detail=detail)


__all__ = [
    "OverviewResponse",
    "OverviewSection",
    "OverviewSources",
    "OverviewUnavailableError",
    "compose_overview",
    "create_router",
]
