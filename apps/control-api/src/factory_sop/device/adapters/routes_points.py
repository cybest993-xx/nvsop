"""点位配置与绑定预检的 HTTP 适配器。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from factory_sop.auth.api import Authorized, Permission, needs
from factory_sop.device.adapters import route_support
from factory_sop.device.adapters.dependencies import connectors, points, stations
from factory_sop.device.model import (
    BindingReasonCode,
    DeviceStatus,
    Point,
    PointDirection,
)
from factory_sop.device.repository import ConnectorRepository, PointRepository, StationRepository
from factory_sop.device.usecases.points import (
    create_point,
    delete_point,
    edit_point,
    list_points,
    point_by_identifier,
    set_point_status,
    validate_binding,
)
from factory_sop.responses import DEFAULT_PAGE_SIZE, MAXIMUM_PAGE_SIZE, ItemPage
from nvsop_contracts import PointRole

router = APIRouter(prefix="/points", tags=["device"], responses=route_support._UNAUTHORIZED)


class PointConfiguration(BaseModel):
    """点位的完整物理位置与工位内语义。"""

    model_config = ConfigDict(extra="forbid")

    station_id: UUID
    connector_id: UUID
    direction: PointDirection
    identifier: str = Field(min_length=1, max_length=128)
    semantic_label: str = Field(min_length=1, max_length=128)


class PointView(BaseModel):
    """中心保存的点位记录。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    station_id: UUID
    connector_id: UUID
    direction: PointDirection
    identifier: str
    semantic_label: str
    status: DeviceStatus
    revision: int
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime


def _view(point: Point) -> PointView:
    return PointView.model_validate(point)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    operation_id="createPoint",
    openapi_extra=needs(Permission.POINT_EDIT),
    responses=route_support._ITEM_RESPONSES,
)
def create_a_point(
    configuration: PointConfiguration,
    caller: Authorized,
    station_store: Annotated[StationRepository, Depends(stations)],
    connector_store: Annotated[ConnectorRepository, Depends(connectors)],
    point_store: Annotated[PointRepository, Depends(points)],
) -> PointView:
    return _view(
        create_point(
            **configuration.model_dump(),
            caller=caller,
            now=datetime.now(UTC),
            stations=station_store,
            connectors=connector_store,
            points=point_store,
        )
    )


@router.get("", operation_id="listPoints", openapi_extra=needs(Permission.POINT_VIEW))
def list_the_points(
    caller: Authorized,
    point_store: Annotated[PointRepository, Depends(points)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    station_id: Annotated[UUID | None, Query()] = None,
    connector_id: Annotated[UUID | None, Query()] = None,
) -> ItemPage[PointView]:
    items, total = list_points(
        caller=caller,
        points=point_store,
        page=page,
        page_size=page_size,
        station_id=station_id,
        connector_id=connector_id,
    )
    return ItemPage(
        items=[_view(point) for point in items],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get(
    "/{point_id}",
    operation_id="readPoint",
    openapi_extra=needs(Permission.POINT_VIEW),
    responses=route_support._NOT_FOUND_RESPONSES,
)
def read_a_point(
    point_id: UUID,
    caller: Authorized,
    point_store: Annotated[PointRepository, Depends(points)],
) -> PointView:
    return _view(point_by_identifier(point_id=point_id, caller=caller, points=point_store))


@router.patch(
    "/{point_id}",
    operation_id="editPoint",
    openapi_extra=needs(Permission.POINT_EDIT),
    responses=route_support._ITEM_RESPONSES,
)
def edit_a_point(
    point_id: UUID,
    configuration: PointConfiguration,
    caller: Authorized,
    station_store: Annotated[StationRepository, Depends(stations)],
    connector_store: Annotated[ConnectorRepository, Depends(connectors)],
    point_store: Annotated[PointRepository, Depends(points)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> PointView:
    return _view(
        edit_point(
            point_id=point_id,
            **configuration.model_dump(),
            expected_revision=if_match,
            caller=caller,
            now=datetime.now(UTC),
            stations=station_store,
            connectors=connector_store,
            points=point_store,
        )
    )


class PointStatus(BaseModel):
    status: DeviceStatus


class BindingValidationRequest(BaseModel):
    """绑定预检所需的工位、点位角色和结果预算。"""

    model_config = ConfigDict(extra="forbid")

    station_id: UUID
    point_id: UUID | None = None
    role: PointRole
    budget_seconds: float = Field(ge=0)


class BindingReasonView(BaseModel):
    """一项可直接展示给操作者的绑定拒绝原因。"""

    code: BindingReasonCode
    field: str
    message: str


class BindingValidationView(BaseModel):
    """绑定预检结果；拒绝原因保持稳定的 code、field、message 结构。"""

    accepted: bool
    reasons: list[BindingReasonView]


binding_validation_router = APIRouter(
    prefix="/point-binding-validations",
    tags=["device"],
    responses=route_support._UNAUTHORIZED,
)


@binding_validation_router.post(
    "",
    operation_id="validatePointBinding",
    openapi_extra=needs(Permission.POINT_VIEW),
    responses=route_support._ITEM_RESPONSES,
)
def validate_a_point_binding(
    request: BindingValidationRequest,
    caller: Authorized,
    station_store: Annotated[StationRepository, Depends(stations)],
    point_store: Annotated[PointRepository, Depends(points)],
    connector_store: Annotated[ConnectorRepository, Depends(connectors)],
) -> BindingValidationView:
    """只校验绑定候选，不创建或修改模板绑定。"""
    validation = validate_binding(
        station_id=request.station_id,
        point_id=request.point_id,
        role=request.role,
        budget=request.budget_seconds,
        caller=caller,
        stations=station_store,
        points=point_store,
        connectors=connector_store,
    )
    return BindingValidationView(
        accepted=validation.accepted,
        reasons=[
            BindingReasonView(code=reason.code, field=reason.field, message=reason.message)
            for reason in validation.reasons
        ],
    )


@router.put(
    "/{point_id}/status",
    operation_id="setPointStatus",
    openapi_extra=needs(Permission.POINT_EDIT),
    responses=route_support._ITEM_RESPONSES,
)
def set_the_point_status(
    point_id: UUID,
    requested: PointStatus,
    caller: Authorized,
    point_store: Annotated[PointRepository, Depends(points)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> PointView:
    return _view(
        set_point_status(
            point_id=point_id,
            requested_status=requested.status,
            expected_revision=if_match,
            caller=caller,
            now=datetime.now(UTC),
            points=point_store,
        )
    )


@router.delete(
    "/{point_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="deletePoint",
    openapi_extra=needs(Permission.POINT_DELETE),
    responses=route_support._ITEM_RESPONSES,
)
def delete_a_point(
    point_id: UUID,
    caller: Authorized,
    point_store: Annotated[PointRepository, Depends(points)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> Response:
    delete_point(
        point_id=point_id,
        expected_revision=if_match,
        caller=caller,
        points=point_store,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
