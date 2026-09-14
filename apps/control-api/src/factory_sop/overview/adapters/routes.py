"""HTTP adapter that composes permission-scoped owner summaries in one UoW."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from factory_sop.auth.api import Authorized
from factory_sop.dataset.adapters import dependencies as dataset_dependencies
from factory_sop.dataset.usecases.summary import summary as dataset_summary
from factory_sop.device.adapters import dependencies as device_dependencies
from factory_sop.device.usecases.summary import summary as device_summary
from factory_sop.monitor.adapters import dependencies as monitor_dependencies
from factory_sop.monitor.repository import MonitorRepository
from factory_sop.monitor.usecases import summary as monitor_summary
from factory_sop.overview.usecases import (
    OverviewUnavailableError,
    build_overview,
)
from factory_sop.persistence import RequestSession
from factory_sop.template.adapters import dependencies as template_dependencies
from factory_sop.template.usecases.summary import summary as template_summary

router = APIRouter(prefix="/overview", tags=["overview"])


class ResourceSummary(BaseModel):
    """Counts of a configured resource, preserving unknown status values."""

    total: int = Field(ge=0)
    active: int = Field(ge=0)
    deactivated: int = Field(ge=0)
    unknown: int = Field(ge=0)
    by_status: dict[str, int] = Field(default_factory=dict)


class BackendSummary(ResourceSummary):
    """Resource counts plus real endpoint connection observations."""

    connection_states: dict[str, int] = Field(default_factory=dict)
    verified: int = Field(ge=0)
    unverified: int = Field(ge=0)


class CameraSummary(ResourceSummary):
    """Camera counts plus the persisted credential-presence flag."""

    credentials_configured: int = Field(ge=0)
    credentials_not_configured: int = Field(ge=0)


class ConnectorSummary(ResourceSummary):
    """Connector counts plus measured reachability, including unverified."""

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


@router.get(
    "",
    operation_id="readOverview",
    response_model=OverviewResponse,
    response_model_exclude_none=True,
)
def read_overview(
    caller: Authorized,
    session: RequestSession,
    monitor: Annotated[MonitorRepository, Depends(monitor_dependencies.monitor)],
) -> dict[str, object]:
    """Compose all owner summaries through the request's one SQLAlchemy session."""
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


def _read_summary(section: str, provider: Callable[[], dict[str, object]]) -> dict[str, object]:
    """Convert known read/pagination failures into a section-level partial failure."""
    try:
        return provider()
    except (SQLAlchemyError, ValueError) as error:
        raise OverviewUnavailableError(section) from error


__all__ = ["OverviewResponse", "router"]
