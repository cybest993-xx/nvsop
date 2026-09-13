"""连接器管理 HTTP 适配器；本阶段不提供测试连接按钮。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    TypeAdapter,
    field_validator,
    model_validator,
)

from factory_sop.auth.api import Authorized, Permission, needs
from factory_sop.device.adapters import route_support
from factory_sop.device.adapters.dependencies import cameras, connectors, hosts, points, stations
from factory_sop.device.model import (
    Connector,
    ConnectorType,
    DeviceStatus,
    contains_connector_credential,
)
from factory_sop.device.repository import (
    CameraRepository,
    ConnectorRepository,
    InferenceHostRepository,
    PointRepository,
    StationRepository,
)
from factory_sop.device.usecases.connectors import (
    connector_by_identifier,
    create_connector,
    delete_connector,
    edit_connector,
    list_connectors,
    set_connector_status,
    update_connector_capability,
)
from factory_sop.responses import DEFAULT_PAGE_SIZE, MAXIMUM_PAGE_SIZE, ItemPage
from nvsop_contracts import capability_from_wire, capability_to_wire

router = APIRouter(prefix="/connectors", tags=["device"], responses=route_support._UNAUTHORIZED)


class ConnectorConfiguration(BaseModel):
    """连接器唯一允许提交的非秘密参数。"""

    model_config = ConfigDict(extra="forbid")

    address: str = Field(min_length=1, max_length=255)
    port: StrictInt | None = Field(default=None, ge=1, le=65535)

    @field_validator("address")
    @classmethod
    def _address_has_no_credentials(cls, value: str) -> str:
        if contains_connector_credential(value):
            raise ValueError("不能携带用户名、密码、查询参数或片段")
        return value


class ConnectorPlacement(BaseModel):
    """连接器的完整配置和拓扑归属。"""

    name: str = Field(min_length=1, max_length=128)
    connector_type: ConnectorType
    configuration: ConnectorConfiguration
    station_id: UUID
    host_id: UUID


class ConnectorStatus(BaseModel):
    status: DeviceStatus


class UnverifiedCapabilityDocument(BaseModel):
    """尚未取得真实测量值的显式声明。"""

    model_config = ConfigDict(extra="forbid")

    verification: Literal["unverified"]


class MeasuredCapabilityDocument(BaseModel):
    """真实设备测得的完整五项能力声明。"""

    model_config = ConfigDict(extra="forbid")

    verification: Literal["measured"]
    delivery: Literal["pushed", "polled"]
    polling_interval_seconds: StrictFloat | None
    max_delivery_delay_seconds: StrictFloat = Field(ge=0)
    sequencing: Literal["sequenced", "unsequenced"]
    edge_preservation: Literal["preserved", "may_drop"]
    timestamp_source: Literal["device_clock", "host_receipt"]

    @model_validator(mode="after")
    def _matches_shared_wire_contract(self) -> Self:
        capability_from_wire(self.model_dump(mode="python"))
        return self


CapabilityDocument = Annotated[
    UnverifiedCapabilityDocument | MeasuredCapabilityDocument,
    Field(discriminator="verification"),
]
_CAPABILITY_DOCUMENT: TypeAdapter[CapabilityDocument] = TypeAdapter(CapabilityDocument)


class ConnectorView(BaseModel):
    """中心保存的连接器视图；凭据只以状态标志出现。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    station_id: UUID
    host_id: UUID
    name: str
    connector_type: ConnectorType
    configuration: ConnectorConfiguration
    credentials_configured: bool
    reachability: str
    health_detail: str | None
    capability: CapabilityDocument | None = None
    status: DeviceStatus
    revision: int
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime


def _view(connector: Connector) -> ConnectorView:
    return ConnectorView(
        id=connector.id,
        station_id=connector.station_id,
        host_id=connector.host_id,
        name=connector.name,
        connector_type=connector.connector_type,
        configuration=ConnectorConfiguration.model_validate(connector.configuration.to_wire()),
        credentials_configured=connector.credentials_configured,
        reachability=connector.reachability.value,
        health_detail=connector.health_detail,
        capability=_CAPABILITY_DOCUMENT.validate_python(capability_to_wire(connector.capability)),
        status=connector.status,
        revision=connector.revision,
        created_by=connector.created_by,
        updated_by=connector.updated_by,
        created_at=connector.created_at,
        updated_at=connector.updated_at,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    operation_id="createConnector",
    openapi_extra=needs(Permission.CONNECTOR_EDIT),
    responses=route_support._CONFLICT_RESPONSES,
)
def create_a_connector(
    placement: ConnectorPlacement,
    caller: Authorized,
    station_store: Annotated[StationRepository, Depends(stations)],
    host_store: Annotated[InferenceHostRepository, Depends(hosts)],
    camera_store: Annotated[CameraRepository, Depends(cameras)],
    connector_store: Annotated[ConnectorRepository, Depends(connectors)],
) -> ConnectorView:
    return _view(
        create_connector(
            station_id=placement.station_id,
            host_id=placement.host_id,
            name=placement.name,
            connector_type=placement.connector_type,
            configuration=placement.configuration.model_dump(exclude_none=True),
            caller=caller,
            now=datetime.now(UTC),
            stations=station_store,
            hosts=host_store,
            cameras=camera_store,
            connectors=connector_store,
        )
    )


@router.get("", operation_id="listConnectors", openapi_extra=needs(Permission.CONNECTOR_VIEW))
def list_the_connectors(
    caller: Authorized,
    connector_store: Annotated[ConnectorRepository, Depends(connectors)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    station_id: Annotated[UUID | None, Query()] = None,
) -> ItemPage[ConnectorView]:
    items, total = list_connectors(
        caller=caller,
        connectors=connector_store,
        page=page,
        page_size=page_size,
        station_id=station_id,
    )
    return ItemPage(
        items=[_view(connector) for connector in items],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get(
    "/{connector_id}",
    operation_id="readConnector",
    openapi_extra=needs(Permission.CONNECTOR_VIEW),
    responses=route_support._NOT_FOUND_RESPONSES,
)
def read_a_connector(
    connector_id: UUID,
    caller: Authorized,
    connector_store: Annotated[ConnectorRepository, Depends(connectors)],
) -> ConnectorView:
    return _view(
        connector_by_identifier(
            connector_id=connector_id, caller=caller, connectors=connector_store
        )
    )


@router.patch(
    "/{connector_id}",
    operation_id="editConnector",
    openapi_extra=needs(Permission.CONNECTOR_EDIT),
    responses=route_support._ITEM_RESPONSES,
)
def edit_a_connector(
    connector_id: UUID,
    placement: ConnectorPlacement,
    caller: Authorized,
    station_store: Annotated[StationRepository, Depends(stations)],
    host_store: Annotated[InferenceHostRepository, Depends(hosts)],
    camera_store: Annotated[CameraRepository, Depends(cameras)],
    connector_store: Annotated[ConnectorRepository, Depends(connectors)],
    point_store: Annotated[PointRepository, Depends(points)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> ConnectorView:
    return _view(
        edit_connector(
            connector_id=connector_id,
            station_id=placement.station_id,
            host_id=placement.host_id,
            name=placement.name,
            connector_type=placement.connector_type,
            configuration=placement.configuration.model_dump(exclude_none=True),
            expected_revision=if_match,
            caller=caller,
            now=datetime.now(UTC),
            stations=station_store,
            hosts=host_store,
            cameras=camera_store,
            connectors=connector_store,
            points=point_store,
        )
    )


@router.put(
    "/{connector_id}/capability",
    operation_id="updateConnectorCapability",
    openapi_extra=needs(Permission.CONNECTOR_EDIT),
    responses=route_support._ITEM_RESPONSES,
)
def update_the_connector_capability(
    connector_id: UUID,
    requested: CapabilityDocument,
    caller: Authorized,
    connector_store: Annotated[ConnectorRepository, Depends(connectors)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> ConnectorView:
    return _view(
        update_connector_capability(
            connector_id=connector_id,
            capability=capability_from_wire(requested.model_dump(mode="python")),
            expected_revision=if_match,
            caller=caller,
            now=datetime.now(UTC),
            connectors=connector_store,
        )
    )


@router.put(
    "/{connector_id}/status",
    operation_id="setConnectorStatus",
    openapi_extra=needs(Permission.CONNECTOR_EDIT),
    responses=route_support._ITEM_RESPONSES,
)
def set_the_connector_status(
    connector_id: UUID,
    requested: ConnectorStatus,
    caller: Authorized,
    connector_store: Annotated[ConnectorRepository, Depends(connectors)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> ConnectorView:
    return _view(
        set_connector_status(
            connector_id=connector_id,
            requested_status=requested.status,
            expected_revision=if_match,
            caller=caller,
            now=datetime.now(UTC),
            connectors=connector_store,
        )
    )


@router.delete(
    "/{connector_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="deleteConnector",
    openapi_extra=needs(Permission.CONNECTOR_DELETE),
    responses=route_support._ITEM_RESPONSES,
)
def delete_a_connector(
    connector_id: UUID,
    caller: Authorized,
    connector_store: Annotated[ConnectorRepository, Depends(connectors)],
    point_store: Annotated[PointRepository, Depends(points)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> Response:
    delete_connector(
        connector_id=connector_id,
        expected_revision=if_match,
        caller=caller,
        connectors=connector_store,
        points=point_store,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
