from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response, status
from pydantic import BaseModel, Field, field_validator

from factory_sop.auth.api import Authorized, Permission, needs
from factory_sop.device.adapters import route_support
from factory_sop.device.adapters.dependencies import cameras, stations
from factory_sop.device.model import DeviceStatus, Station
from factory_sop.device.repository import CameraRepository, StationRepository
from factory_sop.device.usecases.stations import (
    create_station,
    delete_station,
    edit_station,
    list_stations,
    set_station_status,
    station_by_identifier,
)
from factory_sop.responses import DEFAULT_PAGE_SIZE, MAXIMUM_PAGE_SIZE, ItemPage

router = APIRouter(prefix="/stations", tags=["device"], responses=route_support._UNAUTHORIZED)


class StationConfiguration(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    tags: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("tags")
    @classmethod
    def _tags_are_non_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not tag for tag in value):
            raise ValueError("标签不能为空")
        return value


class StationStatus(BaseModel):
    status: DeviceStatus


class StationView(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    code: str
    name: str
    tags: list[str]
    status: DeviceStatus
    revision: int
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime


def _view(station: Station) -> StationView:
    return StationView.model_validate(station)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    operation_id="createStation",
    openapi_extra=needs(Permission.STATION_EDIT),
    responses=route_support._CONFLICT_RESPONSES,
)
def create_a_station(
    configuration: StationConfiguration,
    caller: Authorized,
    station_store: Annotated[StationRepository, Depends(stations)],
) -> StationView:
    return _view(
        create_station(
            **configuration.model_dump(),
            caller=caller,
            now=datetime.now(UTC),
            stations=station_store,
        )
    )


@router.get(
    "",
    operation_id="listStations",
    openapi_extra=needs(Permission.STATION_VIEW),
)
def list_the_stations(
    caller: Authorized,
    station_store: Annotated[StationRepository, Depends(stations)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ItemPage[StationView]:
    items, total = list_stations(
        caller=caller, stations=station_store, page=page, page_size=page_size
    )
    return ItemPage(
        items=[_view(station) for station in items],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get(
    "/{station_id}",
    operation_id="readStation",
    openapi_extra=needs(Permission.STATION_VIEW),
    responses=route_support._NOT_FOUND_RESPONSES,
)
def read_a_station(
    station_id: UUID,
    caller: Authorized,
    station_store: Annotated[StationRepository, Depends(stations)],
) -> StationView:
    return _view(
        station_by_identifier(station_id=station_id, caller=caller, stations=station_store)
    )


@router.patch(
    "/{station_id}",
    operation_id="editStation",
    openapi_extra=needs(Permission.STATION_EDIT),
    responses=route_support._ITEM_RESPONSES,
)
def edit_a_station(
    station_id: UUID,
    configuration: StationConfiguration,
    caller: Authorized,
    station_store: Annotated[StationRepository, Depends(stations)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> StationView:
    return _view(
        edit_station(
            station_id=station_id,
            **configuration.model_dump(),
            expected_revision=if_match,
            caller=caller,
            now=datetime.now(UTC),
            stations=station_store,
        )
    )


@router.put(
    "/{station_id}/status",
    operation_id="setStationStatus",
    openapi_extra=needs(Permission.STATION_EDIT),
    responses=route_support._ITEM_RESPONSES,
)
def set_the_station_status(
    station_id: UUID,
    requested: StationStatus,
    caller: Authorized,
    station_store: Annotated[StationRepository, Depends(stations)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> StationView:
    station = set_station_status(
        station_id=station_id,
        requested_status=requested.status,
        expected_revision=if_match,
        caller=caller,
        now=datetime.now(UTC),
        stations=station_store,
    )
    return _view(station)


@router.delete(
    "/{station_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="deleteStation",
    openapi_extra=needs(Permission.STATION_DELETE),
    responses=route_support._ITEM_RESPONSES,
)
def delete_a_station(
    station_id: UUID,
    caller: Authorized,
    station_store: Annotated[StationRepository, Depends(stations)],
    camera_store: Annotated[CameraRepository, Depends(cameras)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> Response:
    delete_station(
        station_id=station_id,
        expected_revision=if_match,
        caller=caller,
        stations=station_store,
        cameras=camera_store,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
