"""`device` 工位和相机行为通过公开用例接口验证。"""

from __future__ import annotations

from uuid import UUID

import pytest
from auth_fakes import caller_holding
from device_fakes import (
    FAKE_NOW,
    FakeCameras,
    FakeConnectors,
    FakeInferenceBackends,
    FakeInferenceHosts,
    FakeInferenceStations,
    FakePoints,
)

from factory_sop.auth.api import Permission
from factory_sop.auth.authorization import AuthorizationRefusedError
from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.model import DeviceStatus
from factory_sop.device.usecases.cameras import (
    camera_by_identifier,
    create_camera,
    delete_camera,
    edit_camera,
    set_camera_status,
)
from factory_sop.device.usecases.stations import (
    create_station,
    delete_station,
    edit_station,
    set_station_status,
)

CALLER = caller_holding(Permission.STATION_EDIT)


def test_create_station_keeps_its_unique_code_name_tags_and_attribution() -> None:
    stations = FakeInferenceStations()

    station = create_station(
        code="A-001",
        name="装配一号工位",
        tags=("装配", "一线"),
        caller=CALLER,
        now=FAKE_NOW,
        stations=stations,
    )

    assert station.status is DeviceStatus.ACTIVE
    assert station.code == "A-001"
    assert station.name == "装配一号工位"
    assert station.tags == ("装配", "一线")
    assert stations.by_id(station.id) == station
    assert station.created_by == CALLER.user.id


def test_station_crud_has_a_reversible_status_and_delete_keeps_camera_history_guarded() -> None:
    stations = FakeInferenceStations()
    cameras = FakeCameras()
    connectors = FakeConnectors()
    points = FakePoints()
    caller = caller_holding(
        Permission.STATION_VIEW, Permission.STATION_EDIT, Permission.STATION_DELETE
    )
    station = create_station(
        code="A-001", name="装配一号工位", tags=(), caller=caller, now=FAKE_NOW, stations=stations
    )

    edited = edit_station(
        station_id=station.id,
        code="A-002",
        name="装配二号工位",
        tags=("二线",),
        expected_revision=station.revision,
        caller=caller,
        now=FAKE_NOW,
        stations=stations,
    )
    deactivated = set_station_status(
        station_id=edited.id,
        requested_status=DeviceStatus.DEACTIVATED,
        expected_revision=edited.revision,
        caller=caller,
        now=FAKE_NOW,
        stations=stations,
    )
    assert deactivated.status is DeviceStatus.DEACTIVATED
    assert (
        set_station_status(
            station_id=station.id,
            requested_status=DeviceStatus.ACTIVE,
            expected_revision=deactivated.revision,
            caller=caller,
            now=FAKE_NOW,
            stations=stations,
        ).status
        is DeviceStatus.ACTIVE
    )

    # 删除工位时，只要其关联仍可查询，就必须拒绝。
    cameras.register(
        station_id=station.id,
        host_id=UUID(int=1),
        backend_id=UUID(int=2),
    )
    with pytest.raises(DeviceRefusedError) as refused:
        delete_station(
            station_id=station.id,
            expected_revision=deactivated.revision + 1,
            caller=caller,
            stations=stations,
            cameras=cameras,
            connectors=connectors,
            points=points,
        )
    assert refused.value.code is DeviceRefusalCode.STATION_HAS_CAMERAS


def _topology() -> tuple[
    FakeInferenceStations,
    FakeInferenceHosts,
    FakeInferenceBackends,
    FakeCameras,
    FakeConnectors,
]:
    stations = FakeInferenceStations()
    hosts = FakeInferenceHosts()
    backends = FakeInferenceBackends()
    cameras = FakeCameras()
    connectors = FakeConnectors()
    return stations, hosts, backends, cameras, connectors


def test_camera_saves_both_stream_paths_offline_with_credentials_as_status_only() -> None:
    stations, hosts, backends, cameras, connectors = _topology()
    station = stations.register(code="A-001", name="装配一号工位")
    host = hosts.register(name="推理机-1")
    backend = backends.register(host_id=host.id, base_url="http://10.0.8.11:8000")
    caller = caller_holding(Permission.CAMERA_EDIT)

    camera = create_camera(
        name="一号相机",
        address="10.0.8.21",
        main_stream_path="/Streaming/Channels/101",
        sub_stream_path="/Streaming/Channels/102",
        station_id=station.id,
        host_id=host.id,
        backend_id=backend.id,
        caller=caller,
        now=FAKE_NOW,
        stations=stations,
        hosts=hosts,
        backends=backends,
        cameras=cameras,
        connectors=connectors,
    )

    assert camera.credentials_configured is False
    assert (camera.main_stream_path, camera.sub_stream_path) == (
        "/Streaming/Channels/101",
        "/Streaming/Channels/102",
    )
    assert cameras.by_id(camera.id) == camera


def test_camera_use_cases_authorize_each_public_operation() -> None:
    stations, hosts, backends, cameras, _connectors = _topology()
    station = stations.register(code="A-001", name="装配一号工位")
    host = hosts.register(name="推理机-1")
    backend = backends.register(host_id=host.id, base_url="http://10.0.8.11:8000")
    camera = cameras.register(station_id=station.id, host_id=host.id, backend_id=backend.id)

    with pytest.raises(AuthorizationRefusedError):
        camera_by_identifier(camera_id=camera.id, caller=caller_holding(), cameras=cameras)
    with pytest.raises(AuthorizationRefusedError):
        set_camera_status(
            camera_id=camera.id,
            requested_status=DeviceStatus.DEACTIVATED,
            expected_revision=camera.revision,
            caller=caller_holding(Permission.CAMERA_VIEW),
            now=FAKE_NOW,
            cameras=cameras,
        )


def test_camera_rejects_a_host_and_backend_from_different_machines_with_field_errors() -> None:
    stations, hosts, backends, cameras, connectors = _topology()
    station = stations.register(code="A-001", name="装配一号工位")
    host_a = hosts.register(name="推理机-1")
    host_b = hosts.register(name="推理机-2")
    backend = backends.register(host_id=host_b.id, base_url="http://10.0.8.12:8000")

    with pytest.raises(DeviceRefusedError) as refused:
        create_camera(
            name="一号相机",
            address="10.0.8.21",
            main_stream_path="/Streaming/Channels/101",
            sub_stream_path="/Streaming/Channels/102",
            station_id=station.id,
            host_id=host_a.id,
            backend_id=backend.id,
            caller=caller_holding(Permission.CAMERA_EDIT),
            now=FAKE_NOW,
            stations=stations,
            hosts=hosts,
            backends=backends,
            cameras=cameras,
            connectors=connectors,
        )

    assert refused.value.code is DeviceRefusalCode.CAMERA_HOST_BACKEND_MISMATCH
    assert {item.field for item in refused.value.field_errors} == {"host_id", "backend_id"}


def test_cameras_of_one_station_cannot_cross_hosts_or_backend_templates() -> None:
    stations, hosts, backends, cameras, connectors = _topology()
    station = stations.register(code="A-001", name="装配一号工位")
    host_a = hosts.register(name="推理机-1")
    host_b = hosts.register(name="推理机-2")
    backend_a = backends.register(
        host_id=host_a.id, base_url="http://10.0.8.11:8000", template_version_id=UUID(int=101)
    )
    backend_b = backends.register(
        host_id=host_b.id, base_url="http://10.0.8.12:8000", template_version_id=UUID(int=101)
    )
    backend_c = backends.register(
        host_id=host_a.id, base_url="http://10.0.8.11:8001", template_version_id=UUID(int=202)
    )
    cameras.register(station_id=station.id, host_id=host_a.id, backend_id=backend_a.id)

    with pytest.raises(DeviceRefusedError) as host_refused:
        create_camera(
            name="跨机相机",
            address="10.0.8.22",
            main_stream_path="/Streaming/Channels/101",
            sub_stream_path="/Streaming/Channels/102",
            station_id=station.id,
            host_id=host_b.id,
            backend_id=backend_b.id,
            caller=caller_holding(Permission.CAMERA_EDIT),
            now=FAKE_NOW,
            stations=stations,
            hosts=hosts,
            backends=backends,
            cameras=cameras,
            connectors=connectors,
        )
    assert host_refused.value.code is DeviceRefusalCode.CAMERA_STATION_HOST_CONFLICT
    assert host_refused.value.field_errors[0].field == "host_id"

    with pytest.raises(DeviceRefusedError) as template_refused:
        create_camera(
            name="异模板相机",
            address="10.0.8.23",
            main_stream_path="/Streaming/Channels/101",
            sub_stream_path="/Streaming/Channels/102",
            station_id=station.id,
            host_id=host_a.id,
            backend_id=backend_c.id,
            caller=caller_holding(Permission.CAMERA_EDIT),
            now=FAKE_NOW,
            stations=stations,
            hosts=hosts,
            backends=backends,
            cameras=cameras,
            connectors=connectors,
        )
    assert template_refused.value.code is DeviceRefusalCode.CAMERA_STATION_TEMPLATE_CONFLICT
    assert template_refused.value.field_errors[0].field == "backend_id"


def test_camera_deactivation_preserves_association_and_delete_is_separate() -> None:
    stations, hosts, backends, cameras, _connectors = _topology()
    station = stations.register(code="A-001", name="装配一号工位")
    host = hosts.register(name="推理机-1")
    backend = backends.register(host_id=host.id, base_url="http://10.0.8.11:8000")
    camera = cameras.register(station_id=station.id, host_id=host.id, backend_id=backend.id)
    caller = caller_holding(
        Permission.CAMERA_EDIT, Permission.CAMERA_DELETE, Permission.CAMERA_VIEW
    )

    deactivated = set_camera_status(
        camera_id=camera.id,
        requested_status=DeviceStatus.DEACTIVATED,
        expected_revision=camera.revision,
        caller=caller,
        now=FAKE_NOW,
        cameras=cameras,
    )
    assert deactivated.status is DeviceStatus.DEACTIVATED
    assert camera_by_identifier(camera_id=camera.id, caller=caller, cameras=cameras) == deactivated
    restored = set_camera_status(
        camera_id=camera.id,
        requested_status=DeviceStatus.ACTIVE,
        expected_revision=deactivated.revision,
        caller=caller,
        now=FAKE_NOW,
        cameras=cameras,
    )
    assert restored.status is DeviceStatus.ACTIVE
    delete_camera(
        camera_id=camera.id,
        expected_revision=restored.revision,
        caller=caller,
        cameras=cameras,
    )
    assert cameras.by_id(camera.id) is None


def test_edit_camera_refuses_stale_revision_before_changing_streams() -> None:
    stations, hosts, backends, cameras, connectors = _topology()
    station = stations.register(code="A-001", name="装配一号工位")
    host = hosts.register(name="推理机-1")
    backend = backends.register(host_id=host.id, base_url="http://10.0.8.11:8000")
    camera = cameras.register(station_id=station.id, host_id=host.id, backend_id=backend.id)

    with pytest.raises(DeviceRefusedError) as refused:
        edit_camera(
            camera_id=camera.id,
            name=camera.name,
            address=camera.address,
            main_stream_path="/Streaming/Channels/201",
            sub_stream_path=camera.sub_stream_path,
            station_id=station.id,
            host_id=host.id,
            backend_id=backend.id,
            expected_revision=camera.revision + 1,
            caller=caller_holding(Permission.CAMERA_EDIT),
            now=FAKE_NOW,
            stations=stations,
            hosts=hosts,
            backends=backends,
            cameras=cameras,
            connectors=connectors,
        )
    assert refused.value.code is DeviceRefusalCode.STALE_REVISION
    assert cameras.by_id(camera.id) == camera
