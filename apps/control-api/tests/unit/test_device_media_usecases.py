"""相机媒体描述和推理机本地配置导出的用例证据。"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest
from auth_fakes import caller_holding
from device_fakes import (
    FakeCameras,
    FakeInferenceBackends,
    FakeInferenceHosts,
    FakeInferenceStations,
)

from factory_sop.auth.api import Permission
from factory_sop.auth.authorization import AuthorizationRefusedError
from factory_sop.device.model import MediaPathMode, RecordingMode, media_path_for_camera
from factory_sop.device.usecases.media import (
    export_host_media_configuration,
    list_camera_media,
)


def topology() -> tuple[FakeInferenceHosts, FakeInferenceStations, FakeCameras, str]:
    hosts = FakeInferenceHosts()
    stations = FakeInferenceStations()
    cameras = FakeCameras()
    backends = FakeInferenceBackends()
    host = hosts.register(
        name="推理机-媒体",
        mediamtx_address="https://media.example.test:8889",
        mediamtx_playback_address="https://media.example.test:9996",
    )
    station = stations.register(code="media-001", name="媒体工位")
    backend = backends.register(host_id=host.id, base_url="http://backend.example.test:8000")
    camera = cameras.register(station_id=station.id, host_id=host.id, backend_id=backend.id)
    cameras.rows[camera.id] = replace(
        camera,
        media_path_mode=MediaPathMode.CPU_TRANSCODE,
        recording_mode=RecordingMode.PREVIEW_ONLY,
    )
    return hosts, stations, cameras, str(host.id)


def test_camera_media_query_returns_host_address_and_uuid_derived_path() -> None:
    hosts, stations, cameras, _ = topology()

    page, total = list_camera_media(
        caller=caller_holding(Permission.CAMERA_VIEW),
        cameras=cameras,
        stations=stations,
        hosts=hosts,
        page=1,
        page_size=50,
        station_id=None,
    )

    assert total == 1
    assert page[0].host.mediamtx_playback_address == "https://media.example.test:9996"
    assert media_path_for_camera(page[0].camera.id) == f"camera-{page[0].camera.id.hex}"


def test_media_export_requires_both_host_and_camera_view_permissions() -> None:
    hosts, stations, cameras, host_id = topology()

    with pytest.raises(AuthorizationRefusedError):
        export_host_media_configuration(
            host_id=UUID(host_id),
            caller=caller_holding(Permission.INFERENCE_HOST_VIEW),
            cameras=cameras,
            hosts=hosts,
            stations=stations,
        )

    exported = export_host_media_configuration(
        host_id=UUID(host_id),
        caller=caller_holding(Permission.INFERENCE_HOST_VIEW, Permission.CAMERA_VIEW),
        cameras=cameras,
        hosts=hosts,
        stations=stations,
    )
    assert len(exported.cameras) == 1
    assert exported.cameras[0].host.id == exported.host.id
