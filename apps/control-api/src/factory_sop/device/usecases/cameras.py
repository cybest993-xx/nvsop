"""相机 CRUD 及工位/推理机/后端拓扑校验。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.device.errors import DeviceRefusalCode
from factory_sop.device.model import Camera, DeviceStatus, InferenceBackend, InferenceHost, Station
from factory_sop.device.repository import (
    CameraRepository,
    ConnectorRepository,
    InferenceBackendRepository,
    InferenceHostRepository,
    StationRepository,
)
from factory_sop.device.usecases._transitions import refuse, set_status
from factory_sop.identifiers import new_id
from factory_sop.observability import get_logger

_logger = get_logger("device")
_REFUSAL_EVENT = "device.camera.refused"


def create_camera(
    *,
    name: str,
    address: str,
    main_stream_path: str,
    sub_stream_path: str,
    station_id: UUID,
    host_id: UUID,
    backend_id: UUID,
    caller: Caller,
    now: datetime,
    stations: StationRepository,
    hosts: InferenceHostRepository,
    backends: InferenceBackendRepository,
    cameras: CameraRepository,
    connectors: ConnectorRepository,
) -> Camera:
    """以离线安全的未配置凭据状态创建相机。"""
    authorize(caller, Permission.CAMERA_EDIT)
    station, host, backend = _validate_binding(
        station_id=station_id,
        host_id=host_id,
        backend_id=backend_id,
        stations=stations,
        hosts=hosts,
        backends=backends,
        cameras=cameras,
        connectors=connectors,
    )
    camera = Camera(
        id=new_id(),
        name=name,
        address=address,
        main_stream_path=main_stream_path,
        sub_stream_path=sub_stream_path,
        credentials_configured=False,
        station_id=station.id,
        host_id=host.id,
        backend_id=backend.id,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=caller.user.id,
        updated_by=caller.user.id,
        created_at=now,
        updated_at=now,
    )
    cameras.add(camera)
    _logger.info(
        "device.camera.created",
        camera_id=str(camera.id),
        station_id=str(camera.station_id),
        host_id=str(camera.host_id),
        backend_id=str(camera.backend_id),
        actor_id=str(caller.user.id),
    )
    return camera


def edit_camera(
    *,
    camera_id: UUID,
    name: str,
    address: str,
    main_stream_path: str,
    sub_stream_path: str,
    station_id: UUID,
    host_id: UUID,
    backend_id: UUID,
    expected_revision: int,
    caller: Caller,
    now: datetime,
    stations: StationRepository,
    hosts: InferenceHostRepository,
    backends: InferenceBackendRepository,
    cameras: CameraRepository,
    connectors: ConnectorRepository,
) -> Camera:
    """替换相机配置；只有新绑定才要求父级处于活动状态。"""
    authorize(caller, Permission.CAMERA_EDIT)
    camera = _existing_camera(camera_id, cameras)
    _require_revision(camera, expected_revision)
    binding_changed = (station_id, host_id, backend_id) != (
        camera.station_id,
        camera.host_id,
        camera.backend_id,
    )
    if binding_changed:
        station, host, backend = _validate_binding(
            station_id=station_id,
            host_id=host_id,
            backend_id=backend_id,
            stations=stations,
            hosts=hosts,
            backends=backends,
            cameras=cameras,
            connectors=connectors,
            excluding_camera=camera.id,
        )
    else:
        station = _existing_station(station_id, stations)
        host = _existing_host(host_id, hosts)
        backend = _existing_backend(backend_id, backends)
    edited = replace(
        camera,
        name=name,
        address=address,
        main_stream_path=main_stream_path,
        sub_stream_path=sub_stream_path,
        station_id=station.id,
        host_id=host.id,
        backend_id=backend.id,
        revision=expected_revision + 1,
        updated_by=caller.user.id,
        updated_at=now,
    )
    cameras.save(edited, expected_revision=expected_revision)
    _logger.info("device.camera.updated", camera_id=str(camera.id), actor_id=str(caller.user.id))
    return edited


def set_camera_status(
    *,
    camera_id: UUID,
    requested_status: DeviceStatus,
    expected_revision: int,
    caller: Caller,
    now: datetime,
    cameras: CameraRepository,
) -> Camera:
    """通过统一的授权用例接口应用可逆的相机状态。"""
    authorize(caller, Permission.CAMERA_EDIT)
    camera = _existing_camera(camera_id, cameras)
    _require_revision(camera, expected_revision)
    if camera.status is requested_status:
        return camera
    match requested_status:
        case DeviceStatus.DEACTIVATED:
            event = "device.camera.deactivated"
        case DeviceStatus.ACTIVE:
            event = "device.camera.restored"
    return set_status(
        camera,
        status=requested_status,
        event=event,
        actor_id=caller.user.id,
        now=now,
        context={"camera_id": str(camera.id)},
        save=cameras.save,
    )


def delete_camera(
    *, camera_id: UUID, expected_revision: int, caller: Caller, cameras: CameraRepository
) -> None:
    """直接删除一个相机；停用是可逆的替代操作。"""
    authorize(caller, Permission.CAMERA_DELETE)
    camera = _existing_camera(camera_id, cameras)
    _require_revision(camera, expected_revision)
    if not cameras.remove(camera_id, expected_revision=expected_revision):
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.CAMERA_NOT_FOUND,
            camera_id=str(camera_id),
            actor_id=str(caller.user.id),
        )
    _logger.info("device.camera.deleted", camera_id=str(camera_id), actor_id=str(caller.user.id))


def camera_by_identifier(*, camera_id: UUID, caller: Caller, cameras: CameraRepository) -> Camera:
    """读取一个相机及其凭据配置状态。"""
    authorize(caller, Permission.CAMERA_VIEW)
    return _existing_camera(camera_id, cameras)


def list_cameras(
    *, caller: Caller, cameras: CameraRepository, page: int, page_size: int, station_id: UUID | None
) -> tuple[list[Camera], int]:
    """列出相机，可选地限定到一个工位。"""
    authorize(caller, Permission.CAMERA_VIEW)
    return cameras.page_of(page=page, page_size=page_size, station_id=station_id)


def _validate_binding(
    *,
    station_id: UUID,
    host_id: UUID,
    backend_id: UUID,
    stations: StationRepository,
    hosts: InferenceHostRepository,
    backends: InferenceBackendRepository,
    cameras: CameraRepository,
    connectors: ConnectorRepository,
    excluding_camera: UUID | None = None,
) -> tuple[Station, InferenceHost, InferenceBackend]:
    station = _existing_station(station_id, stations)
    host = _existing_host(host_id, hosts)
    backend = _existing_backend(backend_id, backends)
    if station.status is DeviceStatus.DEACTIVATED:
        refuse(_REFUSAL_EVENT, DeviceRefusalCode.STATION_DEACTIVATED, station_id=str(station_id))
    if host.status is DeviceStatus.DEACTIVATED:
        refuse(_REFUSAL_EVENT, DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED, host_id=str(host_id))
    if backend.status is DeviceStatus.DEACTIVATED:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.INFERENCE_BACKEND_DEACTIVATED,
            backend_id=str(backend_id),
        )
    if backend.host_id != host_id:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.CAMERA_HOST_BACKEND_MISMATCH,
            host_id=str(host_id),
            backend_id=str(backend_id),
        )
    for connector in connectors.for_station(station_id):
        if connector.host_id != host_id:
            refuse(
                _REFUSAL_EVENT,
                DeviceRefusalCode.CONNECTOR_STATION_HOST_CONFLICT,
                station_id=str(station_id),
                host_id=str(host_id),
            )
    for existing in cameras.for_station(station_id):
        if existing.id == excluding_camera:
            continue
        if existing.host_id != host_id:
            refuse(
                _REFUSAL_EVENT,
                DeviceRefusalCode.CAMERA_STATION_HOST_CONFLICT,
                station_id=str(station_id),
                host_id=str(host_id),
            )
        existing_backend = backends.by_id(existing.backend_id)
        if existing_backend is not None and (
            existing_backend.template_version_id != backend.template_version_id
        ):
            refuse(
                _REFUSAL_EVENT,
                DeviceRefusalCode.CAMERA_STATION_TEMPLATE_CONFLICT,
                station_id=str(station_id),
                backend_id=str(backend_id),
            )
    return station, host, backend


def _existing_station(station_id: UUID, stations: StationRepository) -> Station:
    station = stations.by_id(station_id)
    if station is None:
        refuse(_REFUSAL_EVENT, DeviceRefusalCode.STATION_NOT_FOUND, station_id=str(station_id))
    return station


def _existing_host(host_id: UUID, hosts: InferenceHostRepository) -> InferenceHost:
    host = hosts.by_id(host_id)
    if host is None:
        refuse(_REFUSAL_EVENT, DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND, host_id=str(host_id))
    return host


def _existing_backend(backend_id: UUID, backends: InferenceBackendRepository) -> InferenceBackend:
    backend = backends.by_id(backend_id)
    if backend is None:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.INFERENCE_BACKEND_NOT_FOUND,
            backend_id=str(backend_id),
        )
    return backend


def _existing_camera(camera_id: UUID, cameras: CameraRepository) -> Camera:
    camera = cameras.by_id(camera_id)
    if camera is None:
        refuse(_REFUSAL_EVENT, DeviceRefusalCode.CAMERA_NOT_FOUND, camera_id=str(camera_id))
    return camera


def _require_revision(camera: Camera, expected_revision: int) -> None:
    if camera.revision != expected_revision:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.STALE_REVISION,
            camera_id=str(camera.id),
            expected_revision=str(expected_revision),
            actual_revision=str(camera.revision),
        )
