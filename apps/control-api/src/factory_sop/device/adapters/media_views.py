"""媒体 API 的非秘密响应模型和稳定的本机 secret 文件引用。"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel

from factory_sop.device.model import (
    DeviceStatus,
    MediaPathMode,
    RecordingMode,
    media_path_for_camera,
)
from factory_sop.device.usecases.media import CameraMediaDescription


class CameraMediaView(BaseModel):
    """Web 预览和回放所需的直接地址；不包含相机用户名或密码。"""

    camera_id: UUID
    camera_name: str
    camera_address: str
    main_stream_path: str
    sub_stream_path: str
    camera_status: DeviceStatus
    camera_revision: int
    station_id: UUID
    station_name: str
    station_status: DeviceStatus
    host_id: UUID
    host_name: str
    backend_id: UUID
    host_status: DeviceStatus
    media_path: str
    media_path_mode: MediaPathMode
    recording_mode: RecordingMode
    credentials_configured: bool
    mediamtx_address: str | None
    mediamtx_playback_address: str | None
    recording_window_seconds: int


class CredentialFileReferences(BaseModel):
    """只包含部署约定的路径，不包含 secret 内容。"""

    username_file: str
    password_file: str


class ExportCameraMediaView(CameraMediaView):
    """推理机本地应用配置；secret 仍由本机只读文件提供。"""

    credential_files: CredentialFileReferences


def camera_media_view(description: CameraMediaDescription) -> CameraMediaView:
    camera = description.camera
    station = description.station
    host = description.host
    return CameraMediaView(
        camera_id=camera.id,
        camera_name=camera.name,
        camera_address=camera.address,
        main_stream_path=camera.main_stream_path,
        sub_stream_path=camera.sub_stream_path,
        camera_status=camera.status,
        camera_revision=camera.revision,
        station_id=station.id,
        station_name=station.name,
        station_status=station.status,
        host_id=host.id,
        host_name=host.name,
        backend_id=camera.backend_id,
        host_status=host.status,
        media_path=media_path_for_camera(camera.id),
        media_path_mode=camera.media_path_mode,
        recording_mode=camera.recording_mode,
        credentials_configured=camera.credentials_configured,
        mediamtx_address=host.mediamtx_address,
        mediamtx_playback_address=host.mediamtx_playback_address,
        recording_window_seconds=host.recording_window_seconds,
    )


def export_camera_media_view(description: CameraMediaDescription) -> ExportCameraMediaView:
    view = camera_media_view(description)
    camera_id = description.camera.id.hex
    return ExportCameraMediaView(
        **view.model_dump(),
        credential_files=CredentialFileReferences(
            username_file=f"/run/secrets/nvsop-camera-{camera_id}-username",
            password_file=f"/run/secrets/nvsop-camera-{camera_id}-password",
        ),
    )
