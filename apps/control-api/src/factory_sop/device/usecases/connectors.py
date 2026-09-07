"""连接器配置的授权 CRUD 与拓扑校验。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import assert_never
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.device.errors import DeviceRefusalCode
from factory_sop.device.model import (
    Connector,
    ConnectorConfiguration,
    ConnectorReachability,
    ConnectorType,
    DeviceStatus,
)
from factory_sop.device.repository import (
    CameraRepository,
    ConnectorRepository,
    InferenceHostRepository,
    StationRepository,
)
from factory_sop.device.usecases._transitions import refuse
from factory_sop.identifiers import new_id
from factory_sop.observability import get_logger

_logger = get_logger("device")
_REFUSAL_EVENT = "device.connector.refused"


def create_connector(
    *,
    station_id: UUID,
    host_id: UUID,
    name: str,
    connector_type: ConnectorType,
    configuration: Mapping[str, object],
    caller: Caller,
    now: datetime,
    stations: StationRepository,
    hosts: InferenceHostRepository,
    connectors: ConnectorRepository,
    cameras: CameraRepository,
) -> Connector:
    """在活动工位和推理机上保存不含凭据的连接器配置。"""
    authorize(caller, Permission.CONNECTOR_EDIT)
    _active_station(station_id, stations)
    _active_host(host_id, hosts)
    _check_station_host(
        station_id,
        host_id,
        cameras=cameras,
        connectors=connectors,
    )
    connector = _build(
        station_id=station_id,
        host_id=host_id,
        name=name,
        connector_type=connector_type,
        configuration=configuration,
        caller_id=caller.user.id,
        now=now,
    )
    connectors.add(connector)
    _logger.info(
        "device.connector.created",
        connector_id=str(connector.id),
        station_id=str(station_id),
        host_id=str(host_id),
        actor_id=str(caller.user.id),
    )
    return connector


def edit_connector(
    *,
    connector_id: UUID,
    station_id: UUID,
    host_id: UUID,
    name: str,
    connector_type: ConnectorType,
    configuration: Mapping[str, object],
    expected_revision: int,
    caller: Caller,
    now: datetime,
    stations: StationRepository,
    hosts: InferenceHostRepository,
    connectors: ConnectorRepository,
    cameras: CameraRepository,
) -> Connector:
    """整体替换连接器配置；迁移拓扑时目标父对象必须活动。"""
    authorize(caller, Permission.CONNECTOR_EDIT)
    current = _existing(connector_id, connectors)
    _require_revision(current, expected_revision)
    placement_changed = (current.station_id, current.host_id) != (station_id, host_id)
    if placement_changed:
        _active_station(station_id, stations)
        _active_host(host_id, hosts)
    else:
        _existing_station(station_id, stations)
        _existing_host(host_id, hosts)
    _check_station_host(
        station_id,
        host_id,
        cameras=cameras,
        connectors=connectors,
        excluding_connector=current.id,
    )
    edited = _build(
        station_id=station_id,
        host_id=host_id,
        name=name,
        connector_type=connector_type,
        configuration=configuration,
        caller_id=caller.user.id,
        now=now,
        existing=current,
    )
    connectors.save(edited, expected_revision=expected_revision)
    _logger.info(
        "device.connector.updated", connector_id=str(connector_id), actor_id=str(caller.user.id)
    )
    return edited


def set_connector_status(
    *,
    connector_id: UUID,
    requested_status: DeviceStatus,
    expected_revision: int,
    caller: Caller,
    now: datetime,
    connectors: ConnectorRepository,
) -> Connector:
    """应用连接器的可逆停用状态。"""
    authorize(caller, Permission.CONNECTOR_EDIT)
    connector = _existing(connector_id, connectors)
    _require_revision(connector, expected_revision)
    if connector.status is requested_status:
        return connector
    match requested_status:
        case DeviceStatus.DEACTIVATED:
            event = "device.connector.deactivated"
        case DeviceStatus.ACTIVE:
            event = "device.connector.restored"
        case _:
            assert_never(requested_status)
    changed = replace(
        connector,
        status=requested_status,
        revision=connector.revision + 1,
        updated_by=caller.user.id,
        updated_at=now,
    )
    connectors.save(changed, expected_revision=connector.revision)
    _logger.info(event, connector_id=str(connector_id), actor_id=str(caller.user.id))
    return changed


def delete_connector(
    *,
    connector_id: UUID,
    expected_revision: int,
    caller: Caller,
    connectors: ConnectorRepository,
) -> None:
    """按版本删除连接器；本阶段没有点位或委托命令子表。"""
    authorize(caller, Permission.CONNECTOR_DELETE)
    connector = _existing(connector_id, connectors)
    _require_revision(connector, expected_revision)
    if not connectors.remove(connector_id, expected_revision=expected_revision):
        refuse(
            _REFUSAL_EVENT, DeviceRefusalCode.CONNECTOR_NOT_FOUND, connector_id=str(connector_id)
        )
    _logger.info(
        "device.connector.deleted", connector_id=str(connector_id), actor_id=str(caller.user.id)
    )


def connector_by_identifier(
    *, connector_id: UUID, caller: Caller, connectors: ConnectorRepository
) -> Connector:
    """读取连接器的非秘密配置和未验证状态。"""
    authorize(caller, Permission.CONNECTOR_VIEW)
    return _existing(connector_id, connectors)


def list_connectors(
    *,
    caller: Caller,
    connectors: ConnectorRepository,
    page: int,
    page_size: int,
    station_id: UUID | None,
) -> tuple[list[Connector], int]:
    """列出连接器，包含停用历史并可按工位筛选。"""
    authorize(caller, Permission.CONNECTOR_VIEW)
    return connectors.page_of(page=page, page_size=page_size, station_id=station_id)


def _build(
    *,
    station_id: UUID,
    host_id: UUID,
    name: str,
    connector_type: ConnectorType,
    configuration: Mapping[str, object],
    caller_id: UUID,
    now: datetime,
    existing: Connector | None = None,
) -> Connector:
    try:
        parsed = ConnectorConfiguration.from_wire(configuration)
    except ValueError:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.CONNECTOR_CONFIGURATION_SECRET,
            actor_id=str(caller_id),
        )
    changed = existing is None or (
        existing.station_id != station_id
        or existing.host_id != host_id
        or existing.name != name
        or existing.connector_type != ConnectorType(connector_type)
        or existing.configuration != parsed
    )
    return Connector(
        id=existing.id if existing is not None else new_id(),
        station_id=station_id,
        host_id=host_id,
        name=name,
        connector_type=ConnectorType(connector_type),
        configuration=parsed,
        credentials_configured=existing.credentials_configured if existing is not None else False,
        reachability=(
            ConnectorReachability.UNVERIFIED
            if changed and existing is not None
            else existing.reachability
            if existing is not None
            else ConnectorReachability.UNVERIFIED
        ),
        health_detail=None if changed else existing.health_detail if existing is not None else None,
        status=existing.status if existing is not None else DeviceStatus.ACTIVE,
        revision=existing.revision + 1 if existing is not None else 1,
        created_by=existing.created_by if existing is not None else caller_id,
        updated_by=caller_id,
        created_at=existing.created_at if existing is not None else now,
        updated_at=now,
    )


def _check_station_host(
    station_id: UUID,
    host_id: UUID,
    *,
    cameras: CameraRepository,
    connectors: ConnectorRepository,
    excluding_connector: UUID | None = None,
) -> None:
    for camera in cameras.for_station(station_id):
        if camera.host_id != host_id:
            refuse(
                _REFUSAL_EVENT,
                DeviceRefusalCode.CONNECTOR_STATION_HOST_CONFLICT,
                station_id=str(station_id),
                host_id=str(host_id),
            )
    for connector in connectors.for_station(station_id):
        if connector.id != excluding_connector and connector.host_id != host_id:
            refuse(
                _REFUSAL_EVENT,
                DeviceRefusalCode.CONNECTOR_STATION_HOST_CONFLICT,
                station_id=str(station_id),
                host_id=str(host_id),
            )


def _existing(connector_id: UUID, connectors: ConnectorRepository) -> Connector:
    connector = connectors.by_id(connector_id)
    if connector is None:
        refuse(
            _REFUSAL_EVENT, DeviceRefusalCode.CONNECTOR_NOT_FOUND, connector_id=str(connector_id)
        )
    return connector


def _existing_station(station_id: UUID, stations: StationRepository) -> None:
    if stations.by_id(station_id) is None:
        refuse(_REFUSAL_EVENT, DeviceRefusalCode.STATION_NOT_FOUND, station_id=str(station_id))


def _active_station(station_id: UUID, stations: StationRepository) -> None:
    _existing_station(station_id, stations)
    station = stations.by_id(station_id)
    if station is not None and station.status is DeviceStatus.DEACTIVATED:
        refuse(_REFUSAL_EVENT, DeviceRefusalCode.STATION_DEACTIVATED, station_id=str(station_id))


def _existing_host(host_id: UUID, hosts: InferenceHostRepository) -> None:
    if hosts.by_id(host_id) is None:
        refuse(_REFUSAL_EVENT, DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND, host_id=str(host_id))


def _active_host(host_id: UUID, hosts: InferenceHostRepository) -> None:
    _existing_host(host_id, hosts)
    host = hosts.by_id(host_id)
    if host is not None and host.status is DeviceStatus.DEACTIVATED:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED,
            host_id=str(host_id),
        )


def _require_revision(connector: Connector, expected_revision: int) -> None:
    if connector.revision != expected_revision:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.STALE_REVISION,
            connector_id=str(connector.id),
            expected_revision=str(expected_revision),
            actual_revision=str(connector.revision),
        )
