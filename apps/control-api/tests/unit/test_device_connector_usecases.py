"""连接器配置用例的行为回归。"""

from __future__ import annotations

from typing import cast

import pytest
from auth_fakes import caller_holding
from device_fakes import (
    FAKE_NOW,
    FakeCameras,
    FakeConnectors,
    FakeInferenceHosts,
    FakeInferenceStations,
)

from factory_sop.auth.api import Permission
from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.model import ConnectorReachability, ConnectorType, DeviceStatus
from factory_sop.device.usecases.connectors import create_connector

CALLER = caller_holding(
    Permission.CONNECTOR_VIEW, Permission.CONNECTOR_EDIT, Permission.CONNECTOR_DELETE
)


def test_authorized_connector_configuration_is_saved_as_unverified() -> None:
    stations = FakeInferenceStations()
    hosts = FakeInferenceHosts()
    connectors = FakeConnectors()
    cameras = FakeCameras()
    station = stations.register(code="A-001", name="装配一号工位")
    host = hosts.register(name="推理机-1")

    connector = create_connector(
        station_id=station.id,
        host_id=host.id,
        name="一号告警口",
        connector_type=ConnectorType.HIKVISION_ISAPI,
        configuration={"address": "10.0.8.21", "port": 80},
        caller=CALLER,
        now=FAKE_NOW,
        stations=stations,
        hosts=hosts,
        connectors=connectors,
        cameras=cameras,
    )

    assert connectors.by_id(connector.id) == connector
    assert connector.station_id == station.id
    assert connector.host_id == host.id
    assert connector.connector_type is ConnectorType.HIKVISION_ISAPI
    assert connector.credentials_configured is False
    assert connector.reachability is ConnectorReachability.UNVERIFIED


def test_connector_edit_status_delete_and_listing_use_the_read_revision() -> None:
    from factory_sop.device.model import DeviceStatus
    from factory_sop.device.usecases.connectors import (
        connector_by_identifier,
        delete_connector,
        edit_connector,
        list_connectors,
        set_connector_status,
    )

    stations = FakeInferenceStations()
    hosts = FakeInferenceHosts()
    connectors = FakeConnectors()
    cameras = FakeCameras()
    station = stations.register(code="A-001", name="装配一号工位")
    host = hosts.register(name="推理机-1")
    connector = create_connector(
        station_id=station.id,
        host_id=host.id,
        name="一号告警口",
        connector_type=ConnectorType.HIKVISION_ISAPI,
        configuration={"address": "10.0.8.21", "port": 80},
        caller=CALLER,
        now=FAKE_NOW,
        stations=stations,
        hosts=hosts,
        connectors=connectors,
        cameras=cameras,
    )

    edited = edit_connector(
        connector_id=connector.id,
        station_id=station.id,
        host_id=host.id,
        name="二号告警口",
        connector_type=ConnectorType.BOARD_CARD,
        configuration={"address": "/dev/board0"},
        expected_revision=1,
        caller=CALLER,
        now=FAKE_NOW,
        stations=stations,
        hosts=hosts,
        connectors=connectors,
        cameras=cameras,
    )
    deactivated = set_connector_status(
        connector_id=edited.id,
        requested_status=DeviceStatus.DEACTIVATED,
        expected_revision=edited.revision,
        caller=CALLER,
        now=FAKE_NOW,
        connectors=connectors,
    )
    restored = set_connector_status(
        connector_id=deactivated.id,
        requested_status=DeviceStatus.ACTIVE,
        expected_revision=deactivated.revision,
        caller=CALLER,
        now=FAKE_NOW,
        connectors=connectors,
    )
    items, total = list_connectors(
        caller=CALLER, connectors=connectors, page=1, page_size=10, station_id=station.id
    )

    assert restored.status is DeviceStatus.ACTIVE
    assert restored.revision == 4
    assert (items, total) == ([restored], 1)
    assert (
        connector_by_identifier(connector_id=restored.id, caller=CALLER, connectors=connectors)
        == restored
    )
    delete_connector(
        connector_id=restored.id,
        expected_revision=restored.revision,
        caller=CALLER,
        connectors=connectors,
    )
    assert connectors.by_id(restored.id) is None


def test_connector_status_does_not_treat_an_unknown_value_as_restored() -> None:
    from factory_sop.device.usecases.connectors import set_connector_status

    stations = FakeInferenceStations()
    hosts = FakeInferenceHosts()
    connectors = FakeConnectors()
    cameras = FakeCameras()
    station = stations.register(code="A-001", name="装配一号工位")
    host = hosts.register(name="推理机-1")
    connector = create_connector(
        station_id=station.id,
        host_id=host.id,
        name="一号告警口",
        connector_type=ConnectorType.HIKVISION_ISAPI,
        configuration={"address": "10.0.8.21"},
        caller=CALLER,
        now=FAKE_NOW,
        stations=stations,
        hosts=hosts,
        connectors=connectors,
        cameras=cameras,
    )

    with pytest.raises(AssertionError):
        set_connector_status(
            connector_id=connector.id,
            requested_status=cast(DeviceStatus, "future_status"),
            expected_revision=connector.revision,
            caller=CALLER,
            now=FAKE_NOW,
            connectors=connectors,
        )

    assert connectors.by_id(connector.id) == connector


def test_connector_configuration_rejects_credentials_and_unknown_fields() -> None:
    stations = FakeInferenceStations()
    hosts = FakeInferenceHosts()
    connectors = FakeConnectors()
    cameras = FakeCameras()
    station = stations.register(code="A-001", name="装配一号工位")
    host = hosts.register(name="推理机-1")

    with pytest.raises(DeviceRefusedError) as refused:
        create_connector(
            station_id=station.id,
            host_id=host.id,
            name="不应保存",
            connector_type=ConnectorType.HIKVISION_ISAPI,
            # 合成凭据，仅验证拒绝路径。
            configuration={
                "address": "http://user:secret@10.0.8.21",  # pragma: allowlist secret
            },
            caller=CALLER,
            now=FAKE_NOW,
            stations=stations,
            hosts=hosts,
            connectors=connectors,
            cameras=cameras,
        )

    assert refused.value.code is DeviceRefusalCode.CONNECTOR_CONFIGURATION_SECRET
    assert connectors.rows == {}

    with pytest.raises(DeviceRefusedError) as raw_refused:
        create_connector(
            station_id=station.id,
            host_id=host.id,
            name="原始凭据地址",
            connector_type=ConnectorType.HIKVISION_ISAPI,
            # 合成凭据，仅验证拒绝路径。
            configuration={
                "address": "user:secret@[10.0.8.21]",  # pragma: allowlist secret
            },
            caller=CALLER,
            now=FAKE_NOW,
            stations=stations,
            hosts=hosts,
            connectors=connectors,
            cameras=cameras,
        )

    assert raw_refused.value.code is DeviceRefusalCode.CONNECTOR_CONFIGURATION_SECRET
    assert connectors.rows == {}

    with pytest.raises(DeviceRefusedError):
        create_connector(
            station_id=station.id,
            host_id=host.id,
            name="未知字段",
            connector_type=ConnectorType.HIKVISION_ISAPI,
            # 合成凭据，仅验证拒绝路径。
            configuration={
                "address": "10.0.8.21",
                "password": "secret",  # pragma: allowlist secret
            },
            caller=CALLER,
            now=FAKE_NOW,
            stations=stations,
            hosts=hosts,
            connectors=connectors,
            cameras=cameras,
        )


def test_connector_cannot_cross_the_existing_station_host_topology() -> None:
    stations = FakeInferenceStations()
    hosts = FakeInferenceHosts()
    connectors = FakeConnectors()
    cameras = FakeCameras()
    station = stations.register(code="A-001", name="装配一号工位")
    host_a = hosts.register(name="推理机-1")
    host_b = hosts.register(name="推理机-2")
    create_connector(
        station_id=station.id,
        host_id=host_a.id,
        name="一号连接器",
        connector_type=ConnectorType.HIKVISION_ISAPI,
        configuration={"address": "10.0.8.21"},
        caller=CALLER,
        now=FAKE_NOW,
        stations=stations,
        hosts=hosts,
        connectors=connectors,
        cameras=cameras,
    )

    with pytest.raises(DeviceRefusedError) as refused:
        create_connector(
            station_id=station.id,
            host_id=host_b.id,
            name="二号连接器",
            connector_type=ConnectorType.BOARD_CARD,
            configuration={"address": "/dev/board0"},
            caller=CALLER,
            now=FAKE_NOW,
            stations=stations,
            hosts=hosts,
            connectors=connectors,
            cameras=cameras,
        )

    assert refused.value.code is DeviceRefusalCode.CONNECTOR_STATION_HOST_CONFLICT
