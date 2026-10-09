"""真实 PostgreSQL/HTTP 验收：主机配置拉取、上报镜像、SSE 与概览。"""

from __future__ import annotations

import importlib
import json
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from _integration_support import build_app, client_for, settings_for
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session as DatabaseSession
from template_fixtures import TemplateFixture, add_template_version, remove_template_versions

from factory_sop.app import API_PREFIX
from factory_sop.auth.permissions import Permission
from factory_sop.configuration.adapters import dependencies as configuration_dependencies
from factory_sop.device.adapters.repository import (
    PostgresInferenceHostRepository,
    PostgresStationRepository,
)
from factory_sop.device.adapters.tables import (
    CameraRow,
    ConnectorRow,
    InferenceBackendRow,
    InferenceHostRow,
    PointRow,
    StationRow,
)
from factory_sop.device.model import (
    Camera,
    ConnectionState,
    Connector,
    ConnectorConfiguration,
    ConnectorReachability,
    ConnectorType,
    DeviceStatus,
    InferenceBackend,
    InferenceHost,
    Point,
    PointDirection,
    Station,
)
from factory_sop.execution.adapters.dependencies import lease_gateway
from factory_sop.execution.api import ExecutionLeaseGateway, StationGrant
from factory_sop.identifiers import new_id
from factory_sop.persistence import RequestSession
from factory_sop.template.adapters.tables import TemplateStationBindingRow, TemplateVersionRow
from factory_sop.template.model import TemplateStationBinding
from nvsop_contracts import (
    DECISION_REPORT_CONTRACT_VERSION,
    HEALTH_REPORT_CAPABILITY,
    HEALTH_REPORT_CONTRACT_VERSION,
    REPORT_CAPABILITIES_HEADER,
    SOP_INSTANCE_REPORT_CAPABILITY,
    SOP_INSTANCE_REPORT_CONTRACT_VERSION,
    ConfigurationBundle,
    HostIdentityKeyPair,
    HostIdentityRequest,
    ReportBackendProvenance,
    ReportedDecision,
    ReportedHealth,
    ReportedObservation,
    ReportedSopInstance,
    ReportEvidence,
    ReportViolation,
    Unverified,
    configuration_from_wire,
    configuration_to_wire,
    generate_host_identity_key_pair,
    reported_decision_to_wire,
    reported_health_to_wire,
    reported_observation_to_wire,
    reported_sop_instance_to_wire,
    sign_host_identity_request,
)

NOW = datetime(2026, 9, 14, 1, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class RuntimeTopology:
    host: InferenceHost
    station: Station
    backend: InferenceBackend
    connector: Connector
    point: Point
    template: TemplateFixture
    identity: HostIdentityKeyPair


@dataclass(frozen=True, slots=True)
class IndependentTopology:
    """另一台独立推理机的完整拓扑，用于证明主机裁剪不会串入他机身份。"""

    host: InferenceHost
    station: Station
    backend: InferenceBackend
    connector: Connector
    point: Point
    camera: Camera
    template: TemplateFixture
    identity: HostIdentityKeyPair


@pytest.fixture
def runtime_topology(engine: Engine) -> Iterator[RuntimeTopology]:
    identity = generate_host_identity_key_pair()
    actor = new_id()
    host = InferenceHost(
        id=new_id(),
        name=f"HTTP 运行推理机-{new_id().hex[:8]}",
        address="10.0.8.201",
        mediamtx_address=None,
        recording_window_seconds=604800,
        disk_watermark_percent=85,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor,
        updated_by=actor,
        created_at=NOW,
        updated_at=NOW,
        identity_public_key=identity.public_key,
    )
    station = Station(
        id=new_id(),
        code=f"HTTP-RUN-{new_id().hex[:8]}",
        name="HTTP 运行验收工位",
        tags=(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor,
        updated_by=actor,
        created_at=NOW,
        updated_at=NOW,
    )
    connector = Connector(
        id=new_id(),
        station_id=station.id,
        host_id=host.id,
        name="HTTP 测试连接器",
        connector_type=ConnectorType.HIKVISION_ISAPI,
        configuration=ConnectorConfiguration(address="10.0.8.202", port=80),
        credentials_configured=False,
        reachability=ConnectorReachability.UNVERIFIED,
        health_detail=None,
        capability=Unverified(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor,
        updated_by=actor,
        created_at=NOW,
        updated_at=NOW,
    )
    point = Point(
        id=new_id(),
        station_id=station.id,
        connector_id=connector.id,
        direction=PointDirection.INPUT,
        identifier="DI-01",
        semantic_label="工件到位",
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor,
        updated_by=actor,
        created_at=NOW,
        updated_at=NOW,
    )
    with DatabaseSession(engine) as session:
        session.add(InferenceHostRow.from_domain(host))
        session.add(StationRow.from_domain(station))
        session.flush()
        template = add_template_version(session, station_id=station.id, now=NOW)
        version = session.get(TemplateVersionRow, template.version_id)
        assert version is not None
        session.add(
            TemplateStationBindingRow.from_domain(
                TemplateStationBinding(
                    id=new_id(),
                    station_id=station.id,
                    desired_version_id=template.version_id,
                    desired_sha256=version.sha256,
                    desired_config_revision=1,
                    revision=1,
                    created_by=actor,
                    updated_by=actor,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        )
        backend = InferenceBackend(
            id=new_id(),
            host_id=host.id,
            base_url="http://10.0.8.201:8000",
            template_version_id=template.version_id,
            status=DeviceStatus.ACTIVE,
            connection_state=ConnectionState.UNVERIFIED,
            connection_checked_at=None,
            connection_detail=None,
            self_reported_model_ids=("reported-model", "reported-model-2"),
            self_reported_at=None,
            revision=1,
            created_by=actor,
            updated_by=actor,
            created_at=NOW,
            updated_at=NOW,
        )
        camera = Camera(
            id=new_id(),
            name="HTTP 运行相机",
            address="10.0.8.203",
            main_stream_path="/Streaming/Channels/101",
            sub_stream_path="/Streaming/Channels/102",
            credentials_configured=False,
            station_id=station.id,
            host_id=host.id,
            backend_id=backend.id,
            status=DeviceStatus.ACTIVE,
            revision=1,
            created_by=actor,
            updated_by=actor,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(InferenceBackendRow.from_domain(backend))
        session.flush()
        session.add(CameraRow.from_domain(camera))
        session.add(ConnectorRow.from_domain(connector))
        session.flush()
        session.add(PointRow.from_domain(point))
        session.commit()
    topology = RuntimeTopology(host, station, backend, connector, point, template, identity)
    try:
        yield topology
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM monitor_reported_decision WHERE host_id = :host_id"),
                {"host_id": str(host.id)},
            )
            connection.execute(
                text("DELETE FROM monitor_reported_health WHERE host_id = :host_id"),
                {"host_id": str(host.id)},
            )
            connection.execute(
                text("DELETE FROM monitor_sop_instance WHERE host_id = :host_id"),
                {"host_id": str(host.id)},
            )
            connection.execute(
                text("DELETE FROM monitor_violation WHERE host_id = :host_id"),
                {"host_id": str(host.id)},
            )
            connection.execute(
                text("DELETE FROM monitor_observation WHERE host_id = :host_id"),
                {"host_id": str(host.id)},
            )
            connection.execute(
                text("DELETE FROM device_point WHERE station_id = :station_id"),
                {"station_id": station.id},
            )
            connection.execute(
                text("DELETE FROM device_camera WHERE station_id = :station_id"),
                {"station_id": station.id},
            )
            connection.execute(
                text("DELETE FROM device_connector WHERE station_id = :station_id"),
                {"station_id": station.id},
            )
            connection.execute(
                text("DELETE FROM template_station_binding WHERE station_id = :station_id"),
                {"station_id": station.id},
            )
            connection.execute(
                text("DELETE FROM device_inference_backend WHERE id = :backend_id"),
                {"backend_id": backend.id},
            )
            remove_template_versions(connection, (template,))
            connection.execute(
                text("DELETE FROM device_station WHERE id = :station_id"),
                {"station_id": station.id},
            )
            connection.execute(
                text("DELETE FROM device_configuration_assignment WHERE host_id = :host_id"),
                {"host_id": host.id},
            )
            connection.execute(
                text("DELETE FROM device_inference_host WHERE id = :host_id"),
                {"host_id": host.id},
            )


def _host_headers(
    topology: RuntimeTopology | IndependentTopology,
    *,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
) -> dict[str, str]:
    timestamp = int(time.time())
    nonce = f"integration-{new_id().hex}"
    identity = HostIdentityRequest(
        method=method,
        path=path,
        host_id=str(topology.host.id),
        timestamp=timestamp,
        nonce=nonce,
        body=body,
    )
    return {
        "X-Inference-Host-ID": str(topology.host.id),
        "X-Inference-Host-Timestamp": str(timestamp),
        "X-Inference-Host-Nonce": nonce,
        "X-Inference-Host-Signature": sign_host_identity_request(
            identity, private_key=topology.identity.private_key
        ),
    }


def _historical_report(
    topology: RuntimeTopology,
    bundle: ConfigurationBundle,
    *,
    event_id: str,
) -> ReportedDecision:
    station = bundle.stations[0]
    assert station.template is not None
    assert station.backend_id is not None
    return ReportedDecision(
        event_id=event_id,
        trace_id=event_id,
        host_id=bundle.host_id,
        station_id=station.station_id,
        backend_id=None,
        instance_id=17,
        verdict="pass",
        reason_codes=(),
        violations=(),
        lifecycle="closed_by_end_signal",
        evidence=ReportEvidence(None, None, None),
        template_version_id=station.template.version_id,
        template_sha256=station.template.version_sha256,
        model_ids=(),
        reported_at="2026-09-14T01:00:00Z",
        backend_provenance=(ReportBackendProvenance(station.backend_id, station.model_ids),),
        configuration_revision=bundle.config_revision,
        configuration_sha256=bundle.effective_sha256,
        contract_version=DECISION_REPORT_CONTRACT_VERSION,
    )


def _rebound_topology(engine: Engine, original: RuntimeTopology) -> RuntimeTopology:
    identity = generate_host_identity_key_pair()
    actor = new_id()
    host = InferenceHost(
        id=new_id(),
        name=f"HTTP 改绑推理机-{new_id().hex[:8]}",
        address="10.0.8.211",
        mediamtx_address=None,
        recording_window_seconds=604800,
        disk_watermark_percent=85,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor,
        updated_by=actor,
        created_at=NOW,
        updated_at=NOW,
        identity_public_key=identity.public_key,
    )
    backend = InferenceBackend(
        id=new_id(),
        host_id=host.id,
        base_url="http://10.0.8.211:8000",
        template_version_id=original.template.version_id,
        status=DeviceStatus.ACTIVE,
        connection_state=ConnectionState.UNVERIFIED,
        connection_checked_at=None,
        connection_detail=None,
        self_reported_model_ids=("rebound-model",),
        self_reported_at=None,
        revision=1,
        created_by=actor,
        updated_by=actor,
        created_at=NOW,
        updated_at=NOW,
    )
    with DatabaseSession(engine) as session:
        session.add(InferenceHostRow.from_domain(host))
        session.flush()
        session.add(InferenceBackendRow.from_domain(backend))
        session.commit()
    return RuntimeTopology(
        host=host,
        station=original.station,
        backend=backend,
        connector=original.connector,
        point=original.point,
        template=original.template,
        identity=identity,
    )


def _rebind_station_to_host_b(
    engine: Engine, original: RuntimeTopology, rebound: RuntimeTopology
) -> None:
    actor = new_id()
    camera = Camera(
        id=new_id(),
        name="HTTP 改绑相机",
        address="10.0.8.213",
        main_stream_path="/Streaming/Channels/101",
        sub_stream_path="/Streaming/Channels/102",
        credentials_configured=False,
        station_id=original.station.id,
        host_id=rebound.host.id,
        backend_id=rebound.backend.id,
        status=DeviceStatus.ACTIVE,
        revision=2,
        created_by=actor,
        updated_by=actor,
        created_at=NOW,
        updated_at=NOW,
    )
    with DatabaseSession(engine) as session:
        session.execute(
            text("DELETE FROM device_point WHERE station_id = :station_id"),
            {"station_id": original.station.id},
        )
        session.execute(
            text("DELETE FROM device_connector WHERE station_id = :station_id"),
            {"station_id": original.station.id},
        )
        session.execute(
            text("DELETE FROM device_camera WHERE station_id = :station_id"),
            {"station_id": original.station.id},
        )
        session.flush()
        session.add(CameraRow.from_domain(camera))
        session.commit()


def _remove_rebound_topology(
    engine: Engine, original: RuntimeTopology, rebound: RuntimeTopology
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM device_camera WHERE station_id = :station_id AND host_id = :host_id"),
            {"station_id": original.station.id, "host_id": rebound.host.id},
        )
        connection.execute(
            text("DELETE FROM device_configuration_assignment WHERE host_id = :host_id"),
            {"host_id": rebound.host.id},
        )
        connection.execute(
            text("DELETE FROM device_inference_backend WHERE id = :backend_id"),
            {"backend_id": rebound.backend.id},
        )
        connection.execute(
            text("DELETE FROM device_inference_host WHERE id = :host_id"),
            {"host_id": rebound.host.id},
        )


def _register_independent_topology(engine: Engine) -> IndependentTopology:
    """插入另一台主机及其独立工位/后端/连接器/点位/相机，供主机裁剪排除断言。"""
    identity = generate_host_identity_key_pair()
    actor = new_id()
    host = InferenceHost(
        id=new_id(),
        name=f"HTTP 独立推理机-{new_id().hex[:8]}",
        address="10.0.8.221",
        mediamtx_address=None,
        recording_window_seconds=604800,
        disk_watermark_percent=85,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor,
        updated_by=actor,
        created_at=NOW,
        updated_at=NOW,
        identity_public_key=identity.public_key,
    )
    station = Station(
        id=new_id(),
        code=f"HTTP-IND-{new_id().hex[:8]}",
        name="HTTP 独立验收工位",
        tags=(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor,
        updated_by=actor,
        created_at=NOW,
        updated_at=NOW,
    )
    connector = Connector(
        id=new_id(),
        station_id=station.id,
        host_id=host.id,
        name="HTTP 独立连接器",
        connector_type=ConnectorType.HIKVISION_ISAPI,
        configuration=ConnectorConfiguration(address="10.0.8.222", port=80),
        credentials_configured=False,
        reachability=ConnectorReachability.UNVERIFIED,
        health_detail=None,
        capability=Unverified(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor,
        updated_by=actor,
        created_at=NOW,
        updated_at=NOW,
    )
    point = Point(
        id=new_id(),
        station_id=station.id,
        connector_id=connector.id,
        direction=PointDirection.INPUT,
        identifier="DI-99",
        semantic_label="独立工件到位",
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor,
        updated_by=actor,
        created_at=NOW,
        updated_at=NOW,
    )
    with DatabaseSession(engine) as session:
        session.add(InferenceHostRow.from_domain(host))
        session.add(StationRow.from_domain(station))
        session.flush()
        template = add_template_version(session, station_id=station.id, now=NOW)
        version = session.get(TemplateVersionRow, template.version_id)
        assert version is not None
        session.add(
            TemplateStationBindingRow.from_domain(
                TemplateStationBinding(
                    id=new_id(),
                    station_id=station.id,
                    desired_version_id=template.version_id,
                    desired_sha256=version.sha256,
                    desired_config_revision=1,
                    revision=1,
                    created_by=actor,
                    updated_by=actor,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        )
        backend = InferenceBackend(
            id=new_id(),
            host_id=host.id,
            base_url="http://10.0.8.221:8000",
            template_version_id=template.version_id,
            status=DeviceStatus.ACTIVE,
            connection_state=ConnectionState.UNVERIFIED,
            connection_checked_at=None,
            connection_detail=None,
            self_reported_model_ids=("independent-model",),
            self_reported_at=None,
            revision=1,
            created_by=actor,
            updated_by=actor,
            created_at=NOW,
            updated_at=NOW,
        )
        camera = Camera(
            id=new_id(),
            name="HTTP 独立相机",
            address="10.0.8.223",
            main_stream_path="/Streaming/Channels/101",
            sub_stream_path="/Streaming/Channels/102",
            credentials_configured=False,
            station_id=station.id,
            host_id=host.id,
            backend_id=backend.id,
            status=DeviceStatus.ACTIVE,
            revision=1,
            created_by=actor,
            updated_by=actor,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(InferenceBackendRow.from_domain(backend))
        session.flush()
        session.add(CameraRow.from_domain(camera))
        session.add(ConnectorRow.from_domain(connector))
        session.flush()
        session.add(PointRow.from_domain(point))
        session.commit()
    return IndependentTopology(host, station, backend, connector, point, camera, template, identity)


def _remove_independent_topology(engine: Engine, topology: IndependentTopology) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM device_point WHERE station_id = :station_id"),
            {"station_id": topology.station.id},
        )
        connection.execute(
            text("DELETE FROM device_camera WHERE station_id = :station_id"),
            {"station_id": topology.station.id},
        )
        connection.execute(
            text("DELETE FROM device_connector WHERE station_id = :station_id"),
            {"station_id": topology.station.id},
        )
        connection.execute(
            text("DELETE FROM template_station_binding WHERE station_id = :station_id"),
            {"station_id": topology.station.id},
        )
        connection.execute(
            text("DELETE FROM device_inference_backend WHERE id = :backend_id"),
            {"backend_id": topology.backend.id},
        )
        remove_template_versions(connection, (topology.template,))
        connection.execute(
            text("DELETE FROM device_station WHERE id = :station_id"),
            {"station_id": topology.station.id},
        )
        connection.execute(
            text("DELETE FROM device_configuration_assignment WHERE host_id = :host_id"),
            {"host_id": topology.host.id},
        )
        connection.execute(
            text("DELETE FROM device_inference_host WHERE id = :host_id"),
            {"host_id": topology.host.id},
        )


@pytest.fixture
def independent_topology(engine: Engine) -> Iterator[IndependentTopology]:
    topology = _register_independent_topology(engine)
    try:
        yield topology
    finally:
        _remove_independent_topology(engine, topology)


def test_configuration_pull_is_host_scoped_and_contains_real_point_address(
    engine: Engine, runtime_topology: RuntimeTopology, dataset_storage_root: Path
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root)
    path = f"{API_PREFIX}/inference-hosts/{runtime_topology.host.id}/configuration"
    with client_for(engine, settings) as client:
        response = client.get(
            path, headers=_host_headers(runtime_topology, method="GET", path=path)
        )

    assert response.status_code == 200
    bundle = configuration_from_wire(response.json())
    assert bundle.host_id == str(runtime_topology.host.id)
    assert bundle.stations[0].runtime_parameters.idle_timeout_seconds == 30.0
    assert bundle.stations[0].model_ids == ("reported-model", "reported-model-2")
    assert bundle.stations[0].points[0].address == "DI-01"
    assert bundle.stations[0].connectors[0].connector_id == str(runtime_topology.connector.id)


def test_configuration_pull_excludes_another_host_topology(
    engine: Engine,
    runtime_topology: RuntimeTopology,
    independent_topology: IndependentTopology,
    dataset_storage_root: Path,
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root)
    own_path = f"{API_PREFIX}/inference-hosts/{runtime_topology.host.id}/configuration"
    other_path = f"{API_PREFIX}/inference-hosts/{independent_topology.host.id}/configuration"
    with client_for(engine, settings) as client:
        own_response = client.get(
            own_path, headers=_host_headers(runtime_topology, method="GET", path=own_path)
        )
        other_response = client.get(
            other_path, headers=_host_headers(independent_topology, method="GET", path=other_path)
        )
        # 主机身份绑定路径：另一台主机的签名不能拉本机配置。
        forged_response = client.get(
            own_path, headers=_host_headers(independent_topology, method="GET", path=own_path)
        )

    assert own_response.status_code == 200
    assert other_response.status_code == 200
    assert forged_response.status_code == 401

    own = configuration_from_wire(own_response.json())
    other = configuration_from_wire(other_response.json())
    assert own.host_id == str(runtime_topology.host.id)
    assert other.host_id == str(independent_topology.host.id)

    own_stations = {station.station_id for station in own.stations}
    own_backends = {station.backend_id for station in own.stations}
    own_connectors = {
        connector.connector_id for station in own.stations for connector in station.connectors
    }
    own_points = {point.point_id for station in own.stations for point in station.points}
    own_cameras = {camera.camera_id for station in own.stations for camera in station.cameras}

    other_stations = {station.station_id for station in other.stations}
    other_backends = {station.backend_id for station in other.stations}
    other_connectors = {
        connector.connector_id for station in other.stations for connector in station.connectors
    }
    other_points = {point.point_id for station in other.stations for point in station.points}
    other_cameras = {camera.camera_id for station in other.stations for camera in station.cameras}

    assert own_stations == {str(runtime_topology.station.id)}
    assert own_backends == {str(runtime_topology.backend.id)}
    assert own_connectors == {str(runtime_topology.connector.id)}
    assert own_points == {str(runtime_topology.point.id)}
    assert len(own_cameras) == 1

    assert other_stations == {str(independent_topology.station.id)}
    assert other_backends == {str(independent_topology.backend.id)}
    assert other_connectors == {str(independent_topology.connector.id)}
    assert other_points == {str(independent_topology.point.id)}
    assert other_cameras == {str(independent_topology.camera.id)}

    assert own_stations.isdisjoint(other_stations)
    assert own_backends.isdisjoint(other_backends)
    assert own_connectors.isdisjoint(other_connectors)
    assert own_points.isdisjoint(other_points)
    assert own_cameras.isdisjoint(other_cameras)


def test_edge_offline_decision_flushes_after_real_center_rebind(
    engine: Engine, runtime_topology: RuntimeTopology, dataset_storage_root: Path
) -> None:
    edge_source = str(Path(__file__).resolve().parents[4] / "apps/edge-runtime/src")
    sys.path.insert(0, edge_source)
    try:
        edge_model = cast(Any, importlib.import_module("edge_runtime.judgment.model"))
        edge_reasons = cast(Any, importlib.import_module("edge_runtime.judgment.reasons"))
        edge_local_state = cast(Any, importlib.import_module("edge_runtime.local_state"))
        edge_queues = cast(Any, importlib.import_module("edge_runtime.local_state.queues"))
        edge_reporting = cast(Any, importlib.import_module("edge_runtime.reporting"))
    finally:
        sys.path.remove(edge_source)
    settings = settings_for(engine, storage_root=dataset_storage_root)
    config_path = f"{API_PREFIX}/inference-hosts/{runtime_topology.host.id}/configuration"
    report_path = f"{API_PREFIX}/monitor/reported-decisions"
    rebound = _rebound_topology(engine, runtime_topology)
    edge_state = edge_local_state.open_local_state(":memory:")
    try:
        with client_for(engine, settings) as client:
            initial = client.get(
                config_path,
                headers=_host_headers(runtime_topology, method="GET", path=config_path),
            )
            assert initial.status_code == 200
            bundle_n = configuration_from_wire(initial.json())
            station_n = bundle_n.stations[0]
            # 模拟 0033 → 0034 升级窗口：旧 Center 只留下 issued revision/digest，
            # 尚无 assignment history。拓扑变化之后必须靠 Edge 冻结的旧 bundle 恢复，
            # 不能根据变化后的当前拓扑反推。
            with engine.begin() as connection:
                issued = connection.execute(
                    text(
                        """
                        SELECT configuration_sha256
                          FROM device_configuration_issue
                         WHERE host_id = :host_id
                           AND configuration_revision = :revision
                        """
                    ),
                    {
                        "host_id": runtime_topology.host.id,
                        "revision": bundle_n.config_revision,
                    },
                ).scalar_one()
                assert issued == bundle_n.effective_sha256
                connection.execute(
                    text(
                        """
                        DELETE FROM device_configuration_assignment
                         WHERE host_id = :host_id
                           AND configuration_revision = :revision
                        """
                    ),
                    {
                        "host_id": runtime_topology.host.id,
                        "revision": bundle_n.config_revision,
                    },
                )
            assert station_n.template is not None
            assert station_n.backend_id is not None
            provenance_n = edge_queues.BackendReportContext(
                station_n.backend_id, station_n.model_ids
            )
            context_n = edge_queues.ReportContext(
                host_id=bundle_n.host_id,
                station_id=station_n.station_id,
                backends=(provenance_n,),
                template_version_id=station_n.template.version_id,
                template_sha256=station_n.template.version_sha256,
                configuration_revision=bundle_n.config_revision,
                configuration_sha256=bundle_n.effective_sha256,
                configuration_json=json.dumps(
                    configuration_to_wire(bundle_n),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            edge_station = edge_state.station(station_n.station_id, report_context=context_n)
            instance = edge_model.Instance(
                instance_id=1,
                opened_at=edge_model.HostInstant(1.0),
                last_observation_at=edge_model.HostInstant(2.0),
            )
            edge_station.commit(
                state=edge_model.JudgmentState(
                    template=edge_model.Template(
                        steps=("step-1",),
                        ordering=edge_model.Ordering.ORDERED,
                        start_signal="step-1",
                    ),
                    parameters=edge_model.RuntimeParameters(idle_timeout=30.0, step_deadline=10.0),
                    next_instance_id=2,
                ),
                decisions=(
                    edge_model.Decision(
                        instance_id=1,
                        verdict=edge_reasons.Verdict.PASS,
                        reasons=(),
                        violations=(),
                        lifecycle=edge_model.Lifecycle.CLOSED_BY_END_SIGNAL,
                        evidence=edge_model.EvidenceSpan.at(edge_model.HostInstant(2.0)),
                    ),
                ),
                evidence=(),
                closed_instances=(instance,),
                report_provenance={1: (provenance_n,)},
                latched_at="2026-10-09T06:00:00Z",
                latched_monotonic=edge_model.HostInstant(2.0),
            )
            (offline_pending,) = edge_station.pending_reports()
            assert offline_pending.context == context_n

            _rebind_station_to_host_b(engine, runtime_topology, rebound)
            changed = client.get(
                config_path,
                headers=_host_headers(runtime_topology, method="GET", path=config_path),
            )
            assert changed.status_code == 200
            bundle_n1 = configuration_from_wire(changed.json())
            assert bundle_n1.config_revision > bundle_n.config_revision
            assert bundle_n1.stations == ()

            class SignedHttpTransport:
                def __init__(self) -> None:
                    self.sent: list[ReportedDecision] = []
                    self.instances: list[ReportedSopInstance] = []

                def send_decision(
                    self,
                    report: ReportedDecision,
                    *,
                    configuration: ConfigurationBundle | None,
                ) -> None:
                    assert configuration is not None
                    confirm_path = (
                        f"{API_PREFIX}/inference-hosts/{runtime_topology.host.id}"
                        "/confirmed-configuration"
                    )
                    confirmed_body = configuration_to_wire(configuration)
                    confirm_headers = _host_headers(
                        runtime_topology,
                        method="POST",
                        path=confirm_path,
                        body=confirmed_body,
                    )
                    confirm_headers[REPORT_CAPABILITIES_HEADER] = (
                        f"{SOP_INSTANCE_REPORT_CAPABILITY},{HEALTH_REPORT_CAPABILITY}"
                    )
                    confirmed = client.post(
                        confirm_path,
                        json=confirmed_body,
                        headers=confirm_headers,
                    )
                    assert confirmed.status_code == 200
                    assert confirmed.json() == {
                        "decision_report_contract_version": DECISION_REPORT_CONTRACT_VERSION,
                        "sop_instance_report_contract_version": (
                            SOP_INSTANCE_REPORT_CONTRACT_VERSION
                        ),
                        "health_report_contract_version": HEALTH_REPORT_CONTRACT_VERSION,
                    }
                    body = reported_decision_to_wire(report)
                    response = client.post(
                        report_path,
                        json=body,
                        headers=_host_headers(
                            runtime_topology,
                            method="POST",
                            path=report_path,
                            body=body,
                        ),
                    )
                    assert response.status_code == 200
                    assert response.json()["accepted"] is True
                    self.sent.append(report)

                def send_instance(
                    self,
                    report: ReportedSopInstance,
                    *,
                    configuration: ConfigurationBundle | None,
                ) -> None:
                    assert configuration is not None
                    instance_path = f"{API_PREFIX}/monitor/reported-instances"
                    body = reported_sop_instance_to_wire(report)
                    response = client.post(
                        instance_path,
                        json=body,
                        headers=_host_headers(
                            runtime_topology,
                            method="POST",
                            path=instance_path,
                            body=body,
                        ),
                    )
                    assert response.status_code == 200
                    assert response.json()["accepted"] is True
                    self.instances.append(report)

            transport = SignedHttpTransport()
            attempts = edge_reporting.HostReportReconciler(
                reports=edge_state.reports(), transport=transport
            ).flush(
                now=edge_model.HostInstant(10.0),
                reported_at="2026-09-16T00:00:00Z",
            )
            assert len(attempts) == 1
            assert attempts[0].sent is True
            assert len(transport.sent) == 1
            assert len(transport.instances) == 1
            assert transport.sent[0].configuration_revision == bundle_n.config_revision
            assert transport.sent[0].backend_id is None
            assert transport.sent[0].backend_provenance == (
                ReportBackendProvenance(station_n.backend_id, station_n.model_ids),
            )
            assert transport.instances[0].configuration_revision == bundle_n.config_revision
            assert transport.instances[0].instance_id == 1
            assert edge_station.pending_reports() == ()

            duplicate_body = reported_decision_to_wire(transport.sent[0])
            duplicate = client.post(
                report_path,
                json=duplicate_body,
                headers=_host_headers(
                    runtime_topology,
                    method="POST",
                    path=report_path,
                    body=duplicate_body,
                ),
            )
            assert duplicate.status_code == 200
            assert duplicate.json()["duplicate"] is True
    finally:
        edge_state.close()
        _remove_rebound_topology(engine, runtime_topology, rebound)


def test_historical_report_survives_rebind_and_rejects_forged_history(
    engine: Engine, runtime_topology: RuntimeTopology, dataset_storage_root: Path
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root)
    config_path = f"{API_PREFIX}/inference-hosts/{runtime_topology.host.id}/configuration"
    report_path = f"{API_PREFIX}/monitor/reported-decisions"
    rebound = _rebound_topology(engine, runtime_topology)
    try:
        with client_for(engine, settings) as client:
            initial = client.get(
                config_path,
                headers=_host_headers(runtime_topology, method="GET", path=config_path),
            )
            assert initial.status_code == 200
            bundle_n = configuration_from_wire(initial.json())
            report = _historical_report(
                runtime_topology,
                bundle_n,
                event_id=f"{runtime_topology.host.id}:historical-rebind",
            )

            _rebind_station_to_host_b(engine, runtime_topology, rebound)

            changed = client.get(
                config_path,
                headers=_host_headers(runtime_topology, method="GET", path=config_path),
            )
            assert changed.status_code == 200
            bundle_n1 = configuration_from_wire(changed.json())
            assert bundle_n1.config_revision > bundle_n.config_revision
            assert bundle_n1.stations == ()

            body = reported_decision_to_wire(report)
            accepted = client.post(
                report_path,
                json=body,
                headers=_host_headers(runtime_topology, method="POST", path=report_path, body=body),
            )
            duplicate = client.post(
                report_path,
                json=body,
                headers=_host_headers(runtime_topology, method="POST", path=report_path, body=body),
            )

            conflicting = reported_decision_to_wire(
                replace(report, verdict="fail", reason_codes=("WRONG_STEP",))
            )
            conflict = client.post(
                report_path,
                json=conflicting,
                headers=_host_headers(
                    runtime_topology,
                    method="POST",
                    path=report_path,
                    body=conflicting,
                ),
            )

            tampered = reported_decision_to_wire(
                replace(
                    report,
                    event_id=f"{runtime_topology.host.id}:tampered-history",
                    configuration_sha256="d" * 64,
                )
            )
            tampered_response = client.post(
                report_path,
                json=tampered,
                headers=_host_headers(
                    runtime_topology, method="POST", path=report_path, body=tampered
                ),
            )

            tampered_revision = reported_decision_to_wire(
                replace(
                    report,
                    event_id=f"{runtime_topology.host.id}:tampered-revision-history",
                    configuration_revision=(report.configuration_revision or 0) + 1,
                )
            )
            tampered_revision_response = client.post(
                report_path,
                json=tampered_revision,
                headers=_host_headers(
                    runtime_topology,
                    method="POST",
                    path=report_path,
                    body=tampered_revision,
                ),
            )

            never_owned = reported_decision_to_wire(
                replace(
                    report,
                    event_id=f"{runtime_topology.host.id}:never-owned-history",
                    station_id=str(new_id()),
                )
            )
            never_owned_response = client.post(
                report_path,
                json=never_owned,
                headers=_host_headers(
                    runtime_topology,
                    method="POST",
                    path=report_path,
                    body=never_owned,
                ),
            )

            borrowed = reported_decision_to_wire(
                replace(
                    report,
                    event_id=f"{rebound.host.id}:borrowed-history",
                    trace_id=f"{rebound.host.id}:borrowed-history",
                    host_id=str(rebound.host.id),
                )
            )
            borrowed_response = client.post(
                report_path,
                json=borrowed,
                headers=_host_headers(rebound, method="POST", path=report_path, body=borrowed),
            )

        assert accepted.status_code == 200
        assert accepted.json()["duplicate"] is False
        assert duplicate.status_code == 200
        assert duplicate.json()["duplicate"] is True
        assert conflict.status_code == 409
        assert tampered_response.status_code == 409
        assert tampered_revision_response.status_code == 409
        assert never_owned_response.status_code == 409
        assert borrowed_response.status_code == 409
    finally:
        _remove_rebound_topology(engine, runtime_topology, rebound)


def test_historical_report_survives_station_and_backend_deactivation(
    engine: Engine, runtime_topology: RuntimeTopology, dataset_storage_root: Path
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root)
    config_path = f"{API_PREFIX}/inference-hosts/{runtime_topology.host.id}/configuration"
    report_path = f"{API_PREFIX}/monitor/reported-decisions"
    with client_for(engine, settings) as client:
        initial = client.get(
            config_path,
            headers=_host_headers(runtime_topology, method="GET", path=config_path),
        )
        assert initial.status_code == 200
        bundle_n = configuration_from_wire(initial.json())
        report = _historical_report(
            runtime_topology,
            bundle_n,
            event_id=f"{runtime_topology.host.id}:historical-deactivated",
        )

        with engine.begin() as connection:
            connection.execute(
                text("UPDATE device_station SET status = 'deactivated' WHERE id = :id"),
                {"id": runtime_topology.station.id},
            )
            connection.execute(
                text("UPDATE device_inference_backend SET status = 'deactivated' WHERE id = :id"),
                {"id": runtime_topology.backend.id},
            )

        changed = client.get(
            config_path,
            headers=_host_headers(runtime_topology, method="GET", path=config_path),
        )
        assert changed.status_code == 200
        bundle_n1 = configuration_from_wire(changed.json())
        assert bundle_n1.config_revision > bundle_n.config_revision
        assert bundle_n1.stations == ()

        body = reported_decision_to_wire(report)
        accepted = client.post(
            report_path,
            json=body,
            headers=_host_headers(runtime_topology, method="POST", path=report_path, body=body),
        )

    assert accepted.status_code == 200
    assert accepted.json()["duplicate"] is False


def test_reported_decision_is_idempotent_and_dashboard_sse_is_a_real_projection(
    engine: Engine, runtime_topology: RuntimeTopology, dataset_storage_root: Path
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root)
    permissions = frozenset({Permission.MONITOR_VIEW})
    report = ReportedDecision(
        event_id=f"{runtime_topology.host.id}:decision-1",
        trace_id="trace-integration-1",
        host_id=str(runtime_topology.host.id),
        station_id=str(runtime_topology.station.id),
        backend_id=str(runtime_topology.backend.id),
        instance_id=7,
        verdict="indeterminate",
        reason_codes=("FUTURE_REASON",),
        violations=(),
        lifecycle="closed_by_run_interruption",
        evidence=ReportEvidence(None, None, None),
        template_version_id=str(runtime_topology.template.version_id),
        template_sha256="a" * 64,
        model_ids=("model-integration",),
        reported_at="2026-09-14T01:00:00Z",
    )
    body = reported_decision_to_wire(report)
    path = f"{API_PREFIX}/monitor/reported-decisions"
    with client_for(engine, settings, permissions=permissions) as client:
        config_path = f"{API_PREFIX}/inference-hosts/{runtime_topology.host.id}/configuration"
        config = client.get(
            config_path, headers=_host_headers(runtime_topology, method="GET", path=config_path)
        )
        assert config.status_code == 200
        bundle = configuration_from_wire(config.json())
        first = client.post(
            path,
            json=body,
            headers=_host_headers(runtime_topology, method="POST", path=path, body=body),
        )
        duplicate = client.post(
            path,
            json=body,
            headers=_host_headers(runtime_topology, method="POST", path=path, body=body),
        )
        health = ReportedHealth(
            event_id=f"{runtime_topology.host.id}:health-1",
            trace_id="trace-health-integration-1",
            host_id=str(runtime_topology.host.id),
            station_id=str(runtime_topology.station.id),
            stream_id="camera-main",
            status="future_status",
            reason_code="FUTURE_HEALTH_REASON",
            detail="synthetic health detail",
            occurred_at="2026-09-14T01:00:00Z",
            source_anchor=1.5,
            anchor_offset=0.25,
            reported_at="2026-09-14T01:00:00Z",
        )
        health_body = reported_health_to_wire(health)
        health_path = f"{API_PREFIX}/monitor/health"
        health_response = client.post(
            health_path,
            json=health_body,
            headers=_host_headers(
                runtime_topology,
                method="POST",
                path=health_path,
                body=health_body,
            ),
        )
        invalid_health = replace(
            health,
            event_id=f"{runtime_topology.host.id}:health-invalid-time",
            trace_id="trace-health-invalid-time",
            occurred_at="invalid",
            source_anchor=None,
            anchor_offset=None,
        )
        invalid_health_body = reported_health_to_wire(invalid_health)
        invalid_health_response = client.post(
            health_path,
            json=invalid_health_body,
            headers=_host_headers(
                runtime_topology,
                method="POST",
                path=health_path,
                body=invalid_health_body,
            ),
        )
        station = bundle.stations[0]
        assert station.template is not None
        long_open_boundary_signal = "s" * 1024
        long_close_boundary_signal = "e" * 1024
        instance = ReportedSopInstance(
            event_id=f"{runtime_topology.host.id}:{runtime_topology.station.id}:instance:7",
            trace_id="trace-instance-integration-7",
            host_id=str(runtime_topology.host.id),
            station_id=str(runtime_topology.station.id),
            instance_id=7,
            opened_at=1.0,
            closed_at=None,
            close_reason=None,
            open_boundary_signal=long_open_boundary_signal,
            close_boundary_signal=None,
            template_version_id=station.template.version_id,
            template_sha256=station.template.version_sha256,
            backend_provenance=(
                ReportBackendProvenance(str(runtime_topology.backend.id), station.model_ids),
            ),
            configuration_revision=bundle.config_revision,
            configuration_sha256=bundle.effective_sha256,
            reported_at="2026-09-14T01:00:00Z",
        )
        instance_body = reported_sop_instance_to_wire(instance)
        instance_path = f"{API_PREFIX}/monitor/reported-instances"
        instance_first = client.post(
            instance_path,
            json=instance_body,
            headers=_host_headers(
                runtime_topology, method="POST", path=instance_path, body=instance_body
            ),
        )
        instance_duplicate = client.post(
            instance_path,
            json=instance_body,
            headers=_host_headers(
                runtime_topology, method="POST", path=instance_path, body=instance_body
            ),
        )
        tampered_close = replace(
            instance,
            opened_at=2.0,
            closed_at=8.0,
            close_reason="closed_by_end_signal",
            close_boundary_signal=long_close_boundary_signal,
            reported_at="2026-09-14T01:00:01Z",
        )
        tampered_close_body = reported_sop_instance_to_wire(tampered_close)
        tampered_close_response = client.post(
            instance_path,
            json=tampered_close_body,
            headers=_host_headers(
                runtime_topology,
                method="POST",
                path=instance_path,
                body=tampered_close_body,
            ),
        )
        closed_instance = replace(
            instance,
            closed_at=8.0,
            close_reason="closed_by_end_signal",
            close_boundary_signal=long_close_boundary_signal,
            reported_at="2026-09-14T01:00:01Z",
        )
        closed_instance_body = reported_sop_instance_to_wire(closed_instance)
        instance_closed = client.post(
            instance_path,
            json=closed_instance_body,
            headers=_host_headers(
                runtime_topology,
                method="POST",
                path=instance_path,
                body=closed_instance_body,
            ),
        )
        delayed_open = client.post(
            instance_path,
            json=instance_body,
            headers=_host_headers(
                runtime_topology, method="POST", path=instance_path, body=instance_body
            ),
        )
        instance_list = client.get(f"{API_PREFIX}/monitor/instances")
        stream = client.get(
            f"{API_PREFIX}/monitor/stream",
            params={"once": "true"},
        )
        stream_health = client.get(
            f"{API_PREFIX}/monitor/stream-health",
            params={"station_id": str(runtime_topology.station.id)},
        )
        host_liveness = client.get(f"{API_PREFIX}/monitor/host-liveness")

    assert first.status_code == 200
    assert first.json() == {"accepted": True, "duplicate": False, "event_id": report.event_id}
    assert duplicate.status_code == 200
    assert duplicate.json() == {"accepted": True, "duplicate": True, "event_id": report.event_id}
    assert health_response.status_code == 200
    assert health_response.json() == {
        "accepted": True,
        "duplicate": False,
        "event_id": health.event_id,
    }
    assert invalid_health_response.status_code == 422
    assert instance_first.status_code == 200
    assert instance_first.json()["duplicate"] is False
    assert instance_duplicate.status_code == 200
    assert instance_duplicate.json()["duplicate"] is True
    assert tampered_close_response.status_code == 409
    assert instance_closed.status_code == 200
    assert instance_closed.json()["duplicate"] is False
    assert delayed_open.status_code == 200
    assert delayed_open.json()["duplicate"] is True
    assert instance_list.status_code == 200
    assert instance_list.json()["page"] == 1
    assert instance_list.json()["page_size"] == 50
    assert instance_list.json()["total"] == 1
    assert instance_list.json()["items"][0] == closed_instance_body
    assert instance_list.json()["items"][0]["open_boundary_signal"] == long_open_boundary_signal
    assert instance_list.json()["items"][0]["close_boundary_signal"] == long_close_boundary_signal
    assert stream.status_code == 200
    assert "event: decision" in stream.text
    assert f"id: {report.event_id}" in stream.text
    assert "FUTURE_REASON" in stream.text
    assert "event: health" in stream.text
    assert health.event_id in stream.text
    assert stream_health.status_code == 200
    assert stream_health.json()["station_id"] == str(runtime_topology.station.id)
    assert stream_health.json()["validity"] == "impaired"
    assert [item["stream_id"] for item in stream_health.json()["streams"]] == ["camera-main"]
    assert host_liveness.status_code == 200
    assert host_liveness.json()["status"] == "available"
    assert [item["host_id"] for item in host_liveness.json()["hosts"]] == [
        str(runtime_topology.host.id)
    ]
    assert "FUTURE_HEALTH_REASON" in stream.text


def test_reported_violation_is_archived_idempotently_and_queryable(
    engine: Engine, runtime_topology: RuntimeTopology, dataset_storage_root: Path
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root)
    path = f"{API_PREFIX}/monitor/reported-decisions"
    violation = ReportViolation(
        reason_code="MISSED_STEP",
        detail="步骤 2 缺失",
        step_ids=("step-2",),
        evidence=ReportEvidence(anchor=12.0, start=11.0, end=12.0),
    )
    report = ReportedDecision(
        event_id=f"{runtime_topology.host.id}:violating-decision",
        trace_id="trace-violation-integration",
        host_id=str(runtime_topology.host.id),
        station_id=str(runtime_topology.station.id),
        backend_id=str(runtime_topology.backend.id),
        instance_id=11,
        verdict="fail",
        reason_codes=("MISSED_STEP",),
        violations=(violation,),
        lifecycle="closed_by_complete_set",
        evidence=ReportEvidence(None, None, None),
        template_version_id=str(runtime_topology.template.version_id),
        template_sha256="a" * 64,
        model_ids=("model-integration",),
        reported_at="2026-09-14T01:00:00Z",
    )
    body = reported_decision_to_wire(report)
    with client_for(engine, settings, permissions=frozenset({Permission.MONITOR_VIEW})) as client:
        first = client.post(
            path,
            json=body,
            headers=_host_headers(runtime_topology, method="POST", path=path, body=body),
        )
        duplicate = client.post(
            path,
            json=body,
            headers=_host_headers(runtime_topology, method="POST", path=path, body=body),
        )
        listed = client.get(f"{API_PREFIX}/monitor/violations")

    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert listed.status_code == 200
    document = listed.json()
    assert document["total"] == 1
    item = document["items"][0]
    assert item == {
        "event_id": f"{report.event_id}#0",
        "decision_event_id": report.event_id,
        "host_id": str(runtime_topology.host.id),
        "station_id": str(runtime_topology.station.id),
        "instance_id": 11,
        "reported_at": report.reported_at,
        "received_at": item["received_at"],
        "violation": violation.to_wire(),
    }
    assert datetime.fromisoformat(item["received_at"]).tzinfo is not None


def test_reported_observation_is_mirrored_idempotently_and_queryable(
    engine: Engine, runtime_topology: RuntimeTopology, dataset_storage_root: Path
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root)
    path = f"{API_PREFIX}/monitor/reported-observations"
    observation = ReportedObservation(
        event_id=f"{runtime_topology.host.id}:observation:1",
        trace_id=f"{runtime_topology.host.id}:observation:1",
        host_id=str(runtime_topology.host.id),
        station_id=str(runtime_topology.station.id),
        instance_id=41,
        source="action",
        signal="(1) step 1",
        source_time=0.5,
        source_anchor=1000.0,
        observed_at=12.0,
        template_version_id=str(runtime_topology.template.version_id),
        template_sha256="a" * 64,
        backend=ReportBackendProvenance(str(runtime_topology.backend.id), ("model-integration",)),
        reported_at="2026-09-14T01:00:00Z",
    )
    body = reported_observation_to_wire(observation)
    with client_for(engine, settings, permissions=frozenset({Permission.MONITOR_VIEW})) as client:
        first = client.post(
            path,
            json=body,
            headers=_host_headers(runtime_topology, method="POST", path=path, body=body),
        )
        duplicate = client.post(
            path,
            json=body,
            headers=_host_headers(runtime_topology, method="POST", path=path, body=body),
        )
        listed = client.get(
            f"{API_PREFIX}/monitor/observations",
            params={
                "station_id": str(runtime_topology.station.id),
                "instance_id": 41,
            },
        )
        other_instance = client.get(
            f"{API_PREFIX}/monitor/observations",
            params={
                "station_id": str(runtime_topology.station.id),
                "instance_id": 42,
            },
        )

    assert first.status_code == 200
    assert first.json() == {
        "accepted": True,
        "duplicate": False,
        "event_id": observation.event_id,
    }
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert listed.status_code == 200
    document = listed.json()
    assert document["total"] == 1
    assert document["items"][0] == body
    assert other_instance.status_code == 200
    assert other_instance.json()["total"] == 0


def test_violation_archive_preserves_open_reason_and_derived_identity(
    engine: Engine, runtime_topology: RuntimeTopology, dataset_storage_root: Path
) -> None:
    """未知长原因码与超过 255 字符的派生事件 id 不得让判定上报失败 (ADR-0003)。"""
    settings = settings_for(engine, storage_root=dataset_storage_root)
    path = f"{API_PREFIX}/monitor/reported-decisions"
    long_reason = "UNKNOWN_" + "R" * 100
    event_id = "e" * 255
    report = ReportedDecision(
        event_id=event_id,
        trace_id=event_id,
        host_id=str(runtime_topology.host.id),
        station_id=str(runtime_topology.station.id),
        backend_id=str(runtime_topology.backend.id),
        instance_id=3,
        verdict="fail",
        reason_codes=(long_reason,),
        violations=(
            ReportViolation(
                reason_code=long_reason,
                detail=None,
                step_ids=("step-9",),
                evidence=ReportEvidence(None, None, None),
            ),
        ),
        lifecycle="closed_by_complete_set",
        evidence=ReportEvidence(None, None, None),
        template_version_id=str(runtime_topology.template.version_id),
        template_sha256="a" * 64,
        model_ids=("model-integration",),
        reported_at="2026-09-14T01:00:00Z",
    )
    body = reported_decision_to_wire(report)
    with client_for(engine, settings, permissions=frozenset({Permission.MONITOR_VIEW})) as client:
        response = client.post(
            path,
            json=body,
            headers=_host_headers(runtime_topology, method="POST", path=path, body=body),
        )
        listed = client.get(f"{API_PREFIX}/monitor/violations")

    assert response.status_code == 200
    assert response.json()["duplicate"] is False
    assert listed.status_code == 200
    (item,) = listed.json()["items"]
    assert item["event_id"] == f"{event_id}#0"
    assert item["violation"]["reason_code"] == long_reason


def test_overview_returns_permission_scoped_real_sections(
    engine: Engine, runtime_topology: RuntimeTopology, dataset_storage_root: Path
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root)
    with client_for(
        engine,
        settings,
        permissions=frozenset({Permission.MONITOR_VIEW}),
    ) as client:
        response = client.get(f"{API_PREFIX}/overview")

    assert response.status_code == 200
    document = response.json()
    assert document["device"] == {"status": "not_permitted", "data": {}}
    assert document["template"] == {"status": "not_permitted", "data": {}}
    assert document["dataset"] == {"status": "not_permitted", "data": {}}
    assert document["monitor"] == {
        "status": "no_data",
        "data": {
            "recent_decisions": 0,
            "recent_health": 0,
            "runtime_status": "reported_observations_only",
        },
    }


def _add_foreign_grant_owner(engine: Engine) -> tuple[InferenceHost, Station]:
    actor = new_id()
    host = InferenceHost(
        id=new_id(),
        name=f"HTTP 他机-{new_id().hex[:8]}",
        address="10.0.8.221",
        mediamtx_address=None,
        recording_window_seconds=604800,
        disk_watermark_percent=85,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor,
        updated_by=actor,
        created_at=NOW,
        updated_at=NOW,
    )
    station = Station(
        id=new_id(),
        code=f"HTTP-OTHER-{new_id().hex[:8]}",
        name="HTTP 他机工位",
        tags=(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor,
        updated_by=actor,
        created_at=NOW,
        updated_at=NOW,
    )
    session = DatabaseSession(engine)
    try:
        PostgresInferenceHostRepository(session).add(host)
        PostgresStationRepository(session).add(station)
        session.commit()
    finally:
        session.close()
    return host, station


def _clear_execution_grants(engine: Engine, station_ids: tuple[UUID, ...]) -> None:
    with engine.begin() as connection:
        expired = datetime(2000, 1, 1, tzinfo=UTC)
        connection.execute(
            text(
                "UPDATE execution_station_grant SET renewed_at = :renewed_at, "
                "lease_expires_at = :lease_expires_at WHERE station_id = ANY(:station_ids)"
            ),
            {
                "renewed_at": expired,
                "lease_expires_at": expired + timedelta(days=7),
                "station_ids": list(station_ids),
            },
        )
        connection.execute(
            text("DELETE FROM execution_station_grant WHERE station_id = ANY(:station_ids)"),
            {"station_ids": list(station_ids)},
        )


def test_configuration_pull_delivers_and_renews_only_the_calling_host_lease(
    engine: Engine, runtime_topology: RuntimeTopology, dataset_storage_root: Path
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root)
    path = f"{API_PREFIX}/inference-hosts/{runtime_topology.host.id}/configuration"
    foreign_host, foreign_station = _add_foreign_grant_owner(engine)
    started = datetime.now(UTC)
    try:
        with DatabaseSession(engine) as session:
            gateway = lease_gateway(session)
            gateway.acquire(
                station_id=runtime_topology.station.id,
                holder_host_id=runtime_topology.host.id,
                request_id=new_id(),
                now=started,
            )
            gateway.acquire(
                station_id=foreign_station.id,
                holder_host_id=foreign_host.id,
                request_id=new_id(),
                now=started,
            )
            session.commit()

        with client_for(engine, settings) as client:
            first = client.get(
                path, headers=_host_headers(runtime_topology, method="GET", path=path)
            )
            second = client.get(
                path, headers=_host_headers(runtime_topology, method="GET", path=path)
            )

        assert first.status_code == 200
        assert second.status_code == 200
        bundle = configuration_from_wire(first.json())
        refreshed = configuration_from_wire(second.json())

        assert len(bundle.execution_grants) == 1
        grant = bundle.execution_grants[0]
        assert grant.station_id == str(runtime_topology.station.id)
        assert grant.holder_host_id == str(runtime_topology.host.id)
        grant_expires_at = datetime.fromisoformat(grant.lease_expires_at.replace("Z", "+00:00"))
        # 成功拉取的边界立即续期：下发的期限晚于请求前签发的七天。
        assert grant_expires_at > started + timedelta(days=7)
        # 同 revision/effective identity 的第二次拉取沿用同一租约身份，不伪造第二份授权。
        refreshed_grant = refreshed.execution_grants[0]
        assert refreshed_grant.grant_id == grant.grant_id
        assert refreshed.effective_sha256 == bundle.effective_sha256
        assert refreshed.config_revision == bundle.config_revision

        with engine.connect() as connection:
            stored = connection.execute(
                text(
                    "SELECT lease_expires_at, renewed_at, holder_host_id "
                    "FROM execution_station_grant WHERE station_id = :station_id"
                ),
                {"station_id": runtime_topology.station.id},
            ).one()
        assert stored.holder_host_id == runtime_topology.host.id
        assert stored.lease_expires_at == datetime.fromisoformat(
            refreshed_grant.lease_expires_at.replace("Z", "+00:00")
        )
        assert stored.lease_expires_at - stored.renewed_at == timedelta(days=7)
    finally:
        _clear_execution_grants(engine, (runtime_topology.station.id, foreign_station.id))
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM device_station WHERE id = :station_id"),
                {"station_id": foreign_station.id},
            )
            connection.execute(
                text("DELETE FROM device_inference_host WHERE id = :host_id"),
                {"host_id": foreign_host.id},
            )


class _RenewalThenFailure:
    """在真实续期写入之后抛错，用来验证拉取边界的事务回滚。"""

    def __init__(self, delegate: ExecutionLeaseGateway) -> None:
        self._delegate = delegate

    def renew_host_leases(
        self, *, host_id: UUID, now: datetime, request_id: UUID
    ) -> tuple[StationGrant, ...]:
        self._delegate.renew_host_leases(host_id=host_id, now=now, request_id=request_id)
        raise RuntimeError("injected failure after lease renewal")


def _faulting_execution_gateway(session: RequestSession) -> ExecutionLeaseGateway:
    return cast(ExecutionLeaseGateway, _RenewalThenFailure(lease_gateway(session)))


def test_configuration_pull_rolls_back_lease_renewal_when_request_fails(
    engine: Engine, runtime_topology: RuntimeTopology, dataset_storage_root: Path
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root)
    path = f"{API_PREFIX}/inference-hosts/{runtime_topology.host.id}/configuration"
    started = datetime.now(UTC)
    acquisition_request = new_id()
    with DatabaseSession(engine) as session:
        lease_gateway(session).acquire(
            station_id=runtime_topology.station.id,
            holder_host_id=runtime_topology.host.id,
            request_id=acquisition_request,
            now=started,
        )
        session.commit()
    try:
        app = build_app(engine, settings)
        app.dependency_overrides[configuration_dependencies.execution_gateway] = (
            _faulting_execution_gateway
        )
        with TestClient(
            app, base_url="https://testserver", raise_server_exceptions=False
        ) as client:
            response = client.get(
                path, headers=_host_headers(runtime_topology, method="GET", path=path)
            )
        assert response.status_code == 500

        with engine.connect() as connection:
            stored = connection.execute(
                text(
                    "SELECT renewed_at, lease_expires_at, request_id "
                    "FROM execution_station_grant WHERE station_id = :station_id"
                ),
                {"station_id": runtime_topology.station.id},
            ).one()
        # 续期先写入随后失败：请求事务整体回滚，授权事实保持请求前状态，不落盘成功期限。
        assert stored.renewed_at == started
        assert stored.lease_expires_at == started + timedelta(days=7)
        assert stored.request_id == acquisition_request
    finally:
        _clear_execution_grants(engine, (runtime_topology.station.id,))


def test_evidence_registration_signed_http_and_idempotent_reconciliation(
    engine: Engine, runtime_topology: RuntimeTopology, dataset_storage_root: Path
) -> None:
    """真实主机签名 HTTP + PG: 重复登记、事实冲突和伪造来源不推进中心状态。"""
    from factory_sop.evidence.adapters.repository import PostgresEvidenceRepository

    settings = settings_for(engine, storage_root=dataset_storage_root)
    path = f"{API_PREFIX}/evidence/registrations"
    evidence_id = f"s034-{runtime_topology.host.id}"
    body: dict[str, object] = {
        "evidence_id": evidence_id,
        "host_id": str(runtime_topology.host.id),
        "station_id": str(runtime_topology.station.id),
        "instance_id": 31,
        "violation_id": None,
        "kind": "clip",
        "origin": "automatic",
        "anchor": 110.0,
        "window_start": 105.0,
        "window_end": 115.0,
        "generation": "original",
        "sha256": "a" * 64,
        "size": 129,
        "reference": "edge-evidence/camera-1.mp4",
    }
    try:
        with client_for(engine, settings) as client:
            first = client.post(
                path,
                json=body,
                headers=_host_headers(runtime_topology, method="POST", path=path, body=body),
            )
            duplicate = client.post(
                path,
                json=body,
                headers=_host_headers(runtime_topology, method="POST", path=path, body=body),
            )
            altered = {**body, "sha256": "b" * 64}
            conflicting = client.post(
                path,
                json=altered,
                headers=_host_headers(runtime_topology, method="POST", path=path, body=altered),
            )
            unsigned = client.post(path, json=body)
            forged = client.post(
                path,
                json=altered,
                headers=_host_headers(runtime_topology, method="POST", path=path, body=body),
            )
        assert first.status_code == 200, first.text
        assert duplicate.status_code == 200, duplicate.text
        assert (
            first.json()
            == duplicate.json()
            == {
                "accepted": True,
                "evidence_id": evidence_id,
                "status": "available",
            }
        )
        assert conflicting.status_code == 409
        assert unsigned.status_code == 401
        assert forged.status_code == 401
        with DatabaseSession(engine) as session:
            saved = PostgresEvidenceRepository(session).find(evidence_id)
            assert saved is not None
            assert saved.registration.sha256 == "a" * 64
            assert saved.registration.reference == body["reference"]
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM evidence_evidence WHERE evidence_id = :id"),
                {"id": evidence_id},
            )
