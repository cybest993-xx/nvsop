"""连接器存在时父对象仍受所属模块的删除保护。"""

from __future__ import annotations

import pytest
from auth_fakes import caller_holding
from device_fakes import (
    FAKE_NOW,
    FakeCameras,
    FakeConnectors,
    FakeInferenceBackends,
    FakeInferenceHosts,
    FakeInferenceStations,
)

from factory_sop.auth.api import Permission
from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.model import Connector, ConnectorType
from factory_sop.device.usecases.connectors import create_connector
from factory_sop.device.usecases.hosts import delete_host
from factory_sop.device.usecases.stations import delete_station

CALLER = caller_holding(
    Permission.CONNECTOR_EDIT,
    Permission.CONNECTOR_DELETE,
    Permission.STATION_DELETE,
    Permission.INFERENCE_HOST_DELETE,
)


def test_parent_deletes_are_guarded_by_the_connector_repository_seam() -> None:
    stations = FakeInferenceStations()
    hosts = FakeInferenceHosts()
    backends = FakeInferenceBackends()
    connectors = FakeConnectors()
    cameras = FakeCameras()
    station = stations.register(code="A-001", name="装配一号工位")
    host = hosts.register(name="推理机-1")
    connector: Connector = create_connector(
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

    with pytest.raises(DeviceRefusedError) as station_refused:
        delete_station(
            station_id=station.id,
            expected_revision=station.revision,
            caller=CALLER,
            stations=stations,
            cameras=FakeCameras(),
            connectors=connectors,
        )
    with pytest.raises(DeviceRefusedError) as host_refused:
        delete_host(
            host_id=host.id,
            expected_revision=host.revision,
            caller=CALLER,
            hosts=hosts,
            backends=backends,
            connectors=connectors,
        )

    assert station_refused.value.code is DeviceRefusalCode.STATION_HAS_CONNECTORS
    assert host_refused.value.code is DeviceRefusalCode.INFERENCE_HOST_HAS_CONNECTORS
    assert connectors.by_id(connector.id) is not None
