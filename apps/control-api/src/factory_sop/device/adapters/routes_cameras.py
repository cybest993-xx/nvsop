from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response, status
from pydantic import BaseModel, Field, field_validator

from factory_sop.auth.api import Authorized, Permission, needs
from factory_sop.device.adapters import route_support
from factory_sop.device.adapters.dependencies import backends, cameras, connectors, hosts, stations
from factory_sop.device.adapters.media_views import CameraMediaView, camera_media_view
from factory_sop.device.model import (
    Camera,
    DeviceStatus,
    MediaPathMode,
    RecordingMode,
    carries_userinfo,
    is_safe_camera_address,
    is_safe_stream_path,
)
from factory_sop.device.repository import (
    CameraRepository,
    ConnectorRepository,
    InferenceBackendRepository,
    InferenceHostRepository,
    StationRepository,
)
from factory_sop.device.usecases.cameras import (
    camera_by_identifier,
    create_camera,
    delete_camera,
    edit_camera,
    list_cameras,
    set_camera_status,
)
from factory_sop.device.usecases.media import camera_media_by_identifier, list_camera_media
from factory_sop.responses import DEFAULT_PAGE_SIZE, MAXIMUM_PAGE_SIZE, ItemPage

router = APIRouter(prefix="/cameras", tags=["device"], responses=route_support._UNAUTHORIZED)


class CameraConfiguration(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    address: str = Field(min_length=1, max_length=255)
    main_stream_path: str = Field(min_length=1, max_length=255)
    sub_stream_path: str = Field(min_length=1, max_length=255)
    station_id: UUID
    host_id: UUID
    backend_id: UUID
    # 编辑时可省略以保留旧策略；创建时由领域默认值补齐。
    media_path_mode: MediaPathMode | None = None
    recording_mode: RecordingMode | None = None

    @field_validator("address", "main_stream_path", "sub_stream_path")
    @classmethod
    def _no_credentials_in_stream_fields(cls, value: str) -> str:
        if carries_userinfo(value) or value != value.strip():
            raise ValueError("不能携带用户名、密码、查询参数或片段")
        return value

    @field_validator("address")
    @classmethod
    def _safe_camera_address(cls, value: str) -> str:
        if not is_safe_camera_address(value):
            raise ValueError("相机地址必须是主机名或 IP，可选端口，不能携带参数")
        return value

    @field_validator("main_stream_path", "sub_stream_path")
    @classmethod
    def _safe_media_path(cls, value: str) -> str:
        if not is_safe_stream_path(value):
            raise ValueError("流地址必须是安全的媒体路径")
        return value


class CameraStatus(BaseModel):
    status: DeviceStatus


class CameraView(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    name: str
    address: str
    main_stream_path: str
    sub_stream_path: str
    credentials_configured: bool
    station_id: UUID
    host_id: UUID
    backend_id: UUID
    status: DeviceStatus
    revision: int
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime


def _view(camera: Camera) -> CameraView:
    return CameraView.model_validate(camera)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    operation_id="createCamera",
    openapi_extra=needs(Permission.CAMERA_EDIT),
    responses=route_support._ITEM_RESPONSES,
)
def create_a_camera(
    configuration: CameraConfiguration,
    caller: Authorized,
    station_store: Annotated[StationRepository, Depends(stations)],
    host_store: Annotated[InferenceHostRepository, Depends(hosts)],
    backend_store: Annotated[InferenceBackendRepository, Depends(backends)],
    camera_store: Annotated[CameraRepository, Depends(cameras)],
    connector_store: Annotated[ConnectorRepository, Depends(connectors)],
) -> CameraView:
    return _view(
        create_camera(
            **configuration.model_dump(),
            caller=caller,
            now=datetime.now(UTC),
            stations=station_store,
            hosts=host_store,
            backends=backend_store,
            cameras=camera_store,
            connectors=connector_store,
        )
    )


@router.get(
    "",
    operation_id="listCameras",
    openapi_extra=needs(Permission.CAMERA_VIEW),
)
def list_the_cameras(
    caller: Authorized,
    camera_store: Annotated[CameraRepository, Depends(cameras)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    station_id: Annotated[UUID | None, Query()] = None,
) -> ItemPage[CameraView]:
    items, total = list_cameras(
        caller=caller,
        cameras=camera_store,
        page=page,
        page_size=page_size,
        station_id=station_id,
    )
    return ItemPage(
        items=[_view(camera) for camera in items],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get(
    "/media",
    operation_id="listCameraMedia",
    openapi_extra=needs(Permission.CAMERA_VIEW),
)
def list_camera_media_descriptions(
    caller: Authorized,
    camera_store: Annotated[CameraRepository, Depends(cameras)],
    station_store: Annotated[StationRepository, Depends(stations)],
    host_store: Annotated[InferenceHostRepository, Depends(hosts)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    station_id: Annotated[UUID | None, Query()] = None,
) -> ItemPage[CameraMediaView]:
    items, total = list_camera_media(
        caller=caller,
        cameras=camera_store,
        stations=station_store,
        hosts=host_store,
        page=page,
        page_size=page_size,
        station_id=station_id,
    )
    return ItemPage(
        items=[camera_media_view(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get(
    "/{camera_id}/media",
    operation_id="readCameraMedia",
    openapi_extra=needs(Permission.CAMERA_VIEW),
    responses=route_support._NOT_FOUND_RESPONSES,
)
def read_a_camera_media(
    camera_id: UUID,
    caller: Authorized,
    camera_store: Annotated[CameraRepository, Depends(cameras)],
    station_store: Annotated[StationRepository, Depends(stations)],
    host_store: Annotated[InferenceHostRepository, Depends(hosts)],
) -> CameraMediaView:
    return camera_media_view(
        camera_media_by_identifier(
            camera_id=camera_id,
            caller=caller,
            cameras=camera_store,
            stations=station_store,
            hosts=host_store,
        )
    )


@router.get(
    "/{camera_id}",
    operation_id="readCamera",
    openapi_extra=needs(Permission.CAMERA_VIEW),
    responses=route_support._NOT_FOUND_RESPONSES,
)
def read_a_camera(
    camera_id: UUID,
    caller: Authorized,
    camera_store: Annotated[CameraRepository, Depends(cameras)],
) -> CameraView:
    return _view(camera_by_identifier(camera_id=camera_id, caller=caller, cameras=camera_store))


@router.patch(
    "/{camera_id}",
    operation_id="editCamera",
    openapi_extra=needs(Permission.CAMERA_EDIT),
    responses=route_support._ITEM_RESPONSES,
)
def edit_a_camera(
    camera_id: UUID,
    configuration: CameraConfiguration,
    caller: Authorized,
    station_store: Annotated[StationRepository, Depends(stations)],
    host_store: Annotated[InferenceHostRepository, Depends(hosts)],
    backend_store: Annotated[InferenceBackendRepository, Depends(backends)],
    camera_store: Annotated[CameraRepository, Depends(cameras)],
    connector_store: Annotated[ConnectorRepository, Depends(connectors)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> CameraView:
    return _view(
        edit_camera(
            camera_id=camera_id,
            **configuration.model_dump(),
            expected_revision=if_match,
            caller=caller,
            now=datetime.now(UTC),
            stations=station_store,
            hosts=host_store,
            backends=backend_store,
            cameras=camera_store,
            connectors=connector_store,
        )
    )


@router.put(
    "/{camera_id}/status",
    operation_id="setCameraStatus",
    openapi_extra=needs(Permission.CAMERA_EDIT),
    responses=route_support._ITEM_RESPONSES,
)
def set_the_camera_status(
    camera_id: UUID,
    requested: CameraStatus,
    caller: Authorized,
    station_store: Annotated[StationRepository, Depends(stations)],
    host_store: Annotated[InferenceHostRepository, Depends(hosts)],
    backend_store: Annotated[InferenceBackendRepository, Depends(backends)],
    camera_store: Annotated[CameraRepository, Depends(cameras)],
    connector_store: Annotated[ConnectorRepository, Depends(connectors)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> CameraView:
    camera = set_camera_status(
        camera_id=camera_id,
        requested_status=requested.status,
        expected_revision=if_match,
        caller=caller,
        now=datetime.now(UTC),
        stations=station_store,
        hosts=host_store,
        backends=backend_store,
        cameras=camera_store,
        connectors=connector_store,
    )
    return _view(camera)


@router.delete(
    "/{camera_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="deleteCamera",
    openapi_extra=needs(Permission.CAMERA_DELETE),
    responses=route_support._ITEM_RESPONSES,
)
def delete_a_camera(
    camera_id: UUID,
    caller: Authorized,
    camera_store: Annotated[CameraRepository, Depends(cameras)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> Response:
    delete_camera(
        camera_id=camera_id,
        expected_revision=if_match,
        caller=caller,
        cameras=camera_store,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
