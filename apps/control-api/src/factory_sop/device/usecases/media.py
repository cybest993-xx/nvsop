"""相机媒体描述和推理机本地媒体配置导出。"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.model import Camera, InferenceHost, Station
from factory_sop.device.repository import (
    CameraRepository,
    InferenceHostRepository,
    StationRepository,
)


@dataclass(frozen=True, slots=True)
class CameraMediaDescription:
    """一条不含凭据的媒体描述；父级名称来自中心当前拓扑。"""

    camera: Camera
    station: Station
    host: InferenceHost


@dataclass(frozen=True, slots=True)
class HostMediaConfiguration:
    """供部署人员在指定推理机本地应用的非秘密配置。"""

    host: InferenceHost
    cameras: tuple[CameraMediaDescription, ...]


def list_camera_media(
    *,
    caller: Caller,
    cameras: CameraRepository,
    stations: StationRepository,
    hosts: InferenceHostRepository,
    page: int,
    page_size: int,
    station_id: UUID | None,
) -> tuple[list[CameraMediaDescription], int]:
    """按相机查看权限返回媒体描述，不把选择哪台主机交给 Web。"""
    authorize(caller, Permission.CAMERA_VIEW)
    page_items, total = cameras.page_of(page=page, page_size=page_size, station_id=station_id)
    return [_describe(camera, stations=stations, hosts=hosts) for camera in page_items], total


def camera_media_by_identifier(
    *,
    camera_id: UUID,
    caller: Caller,
    cameras: CameraRepository,
    stations: StationRepository,
    hosts: InferenceHostRepository,
) -> CameraMediaDescription:
    """读取一条相机媒体描述。"""
    authorize(caller, Permission.CAMERA_VIEW)
    camera = cameras.by_id(camera_id)
    if camera is None:
        raise DeviceRefusedError(DeviceRefusalCode.CAMERA_NOT_FOUND)
    return _describe(camera, stations=stations, hosts=hosts)


def export_host_media_configuration(
    *,
    host_id: UUID,
    caller: Caller,
    cameras: CameraRepository,
    hosts: InferenceHostRepository,
    stations: StationRepository,
) -> HostMediaConfiguration:
    """要求推理机和相机查看权限，导出仅属于该主机的相机配置。"""
    authorize(caller, Permission.INFERENCE_HOST_VIEW)
    authorize(caller, Permission.CAMERA_VIEW)
    host = hosts.by_id(host_id)
    if host is None:
        raise DeviceRefusedError(DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND)
    descriptions = tuple(
        _describe(camera, stations=stations, hosts=hosts) for camera in cameras.for_host(host_id)
    )
    return HostMediaConfiguration(host=host, cameras=descriptions)


def _describe(
    camera: Camera,
    *,
    stations: StationRepository,
    hosts: InferenceHostRepository,
) -> CameraMediaDescription:
    station = stations.by_id(camera.station_id)
    if station is None:
        raise DeviceRefusedError(DeviceRefusalCode.STATION_NOT_FOUND)
    host = hosts.by_id(camera.host_id)
    if host is None:
        raise DeviceRefusedError(DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND)
    return CameraMediaDescription(camera=camera, station=station, host=host)
