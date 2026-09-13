"""工位配置用例。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.device.errors import DeviceRefusalCode
from factory_sop.device.model import DeviceStatus, Station
from factory_sop.device.repository import (
    CameraRepository,
    ConnectorRepository,
    PointRepository,
    StationRepository,
)
from factory_sop.device.usecases._transitions import refuse, set_status
from factory_sop.identifiers import new_id
from factory_sop.observability import get_logger

_logger = get_logger("device")
_REFUSAL_EVENT = "device.station.refused"


def create_station(
    *,
    code: str,
    name: str,
    tags: tuple[str, ...],
    caller: Caller,
    now: datetime,
    stations: StationRepository,
) -> Station:
    """创建一个使用唯一编码、可选标签且处于活动状态的工位。"""
    authorize(caller, Permission.STATION_EDIT)
    station = Station(
        id=new_id(),
        code=code,
        name=name,
        tags=tags,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=caller.user.id,
        updated_by=caller.user.id,
        created_at=now,
        updated_at=now,
    )
    stations.add(station)
    _logger.info("device.station.created", station_id=str(station.id), actor_id=str(caller.user.id))
    return station


def edit_station(
    *,
    station_id: UUID,
    code: str,
    name: str,
    tags: tuple[str, ...],
    expected_revision: int,
    caller: Caller,
    now: datetime,
    stations: StationRepository,
) -> Station:
    """在调用方读取的版本号上替换工位的可编辑字段。"""
    authorize(caller, Permission.STATION_EDIT)
    station = _existing_station(station_id, stations)
    _require_revision(station, expected_revision)
    edited = replace(
        station,
        code=code,
        name=name,
        tags=tags,
        revision=expected_revision + 1,
        updated_by=caller.user.id,
        updated_at=now,
    )
    stations.save(edited, expected_revision=expected_revision)
    _logger.info("device.station.updated", station_id=str(station.id), actor_id=str(caller.user.id))
    return edited


def set_station_status(
    *,
    station_id: UUID,
    requested_status: DeviceStatus,
    expected_revision: int,
    caller: Caller,
    now: datetime,
    stations: StationRepository,
) -> Station:
    """通过统一的授权用例接口应用可逆的工位状态。"""
    authorize(caller, Permission.STATION_EDIT)
    station = _existing_station(station_id, stations)
    _require_revision(station, expected_revision)
    if station.status is requested_status:
        return station
    match requested_status:
        case DeviceStatus.DEACTIVATED:
            event = "device.station.deactivated"
        case DeviceStatus.ACTIVE:
            event = "device.station.restored"
    return set_status(
        station,
        status=requested_status,
        event=event,
        actor_id=caller.user.id,
        now=now,
        context={"station_id": str(station.id)},
        save=stations.save,
    )


def delete_station(
    *,
    station_id: UUID,
    expected_revision: int,
    caller: Caller,
    stations: StationRepository,
    cameras: CameraRepository,
    connectors: ConnectorRepository,
    points: PointRepository,
) -> None:
    """仅在移除其点位、相机和连接器关联后删除工位。"""
    authorize(caller, Permission.STATION_DELETE)
    station = _existing_station(station_id, stations)
    _require_revision(station, expected_revision)
    if points.any_for_station(station_id):
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.STATION_HAS_POINTS,
            station_id=str(station_id),
            actor_id=str(caller.user.id),
        )
    if cameras.any_for_station(station_id):
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.STATION_HAS_CAMERAS,
            station_id=str(station_id),
            actor_id=str(caller.user.id),
        )
    if connectors.any_for_station(station_id):
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.STATION_HAS_CONNECTORS,
            station_id=str(station_id),
            actor_id=str(caller.user.id),
        )
    if not stations.remove(station_id, expected_revision=expected_revision):
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.STATION_NOT_FOUND,
            station_id=str(station_id),
            actor_id=str(caller.user.id),
        )
    _logger.info("device.station.deleted", station_id=str(station_id), actor_id=str(caller.user.id))


def station_by_identifier(
    *, station_id: UUID, caller: Caller, stations: StationRepository
) -> Station:
    """读取一个工位；公开标识不存在时拒绝。"""
    authorize(caller, Permission.STATION_VIEW)
    return _existing_station(station_id, stations)


def list_stations(
    *, caller: Caller, stations: StationRepository, page: int, page_size: int
) -> tuple[list[Station], int]:
    """列出工位，包括已停用的记录以便恢复。"""
    authorize(caller, Permission.STATION_VIEW)
    return stations.page_of(page=page, page_size=page_size)


def _existing_station(station_id: UUID, stations: StationRepository) -> Station:
    station = stations.by_id(station_id)
    if station is None:
        refuse(_REFUSAL_EVENT, DeviceRefusalCode.STATION_NOT_FOUND, station_id=str(station_id))
    return station


def _require_revision(station: Station, expected_revision: int) -> None:
    if station.revision != expected_revision:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.STALE_REVISION,
            station_id=str(station.id),
            expected_revision=str(expected_revision),
            actual_revision=str(station.revision),
        )
