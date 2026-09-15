"""真实 PostgreSQL/HTTP 验收：主机配置拉取、上报镜像、SSE 与概览。"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from _integration_support import client_for, settings_for
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session as DatabaseSession
from template_fixtures import TemplateFixture, add_template_version, remove_template_versions

from factory_sop.app import API_PREFIX
from factory_sop.auth.permissions import Permission
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
from factory_sop.identifiers import new_id
from factory_sop.template.adapters.tables import TemplateStationBindingRow, TemplateVersionRow
from factory_sop.template.model import TemplateStationBinding
from nvsop_contracts import (
    HostIdentityKeyPair,
    HostIdentityRequest,
    ReportedDecision,
    ReportedHealth,
    ReportEvidence,
    Unverified,
    configuration_from_wire,
    generate_host_identity_key_pair,
    reported_decision_to_wire,
    reported_health_to_wire,
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
                text("DELETE FROM device_inference_host WHERE id = :host_id"),
                {"host_id": host.id},
            )


def _host_headers(
    topology: RuntimeTopology,
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


def test_configuration_pull_is_host_scoped_and_contains_real_point_address(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    settings = settings_for(engine)
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


def test_reported_decision_is_idempotent_and_dashboard_sse_is_a_real_projection(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    settings = settings_for(engine)
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
            status="future_status",
            reason_code="FUTURE_HEALTH_REASON",
            detail="synthetic health detail",
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
        stream = client.get(
            f"{API_PREFIX}/monitor/stream",
            params={"once": "true"},
        )

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
    assert stream.status_code == 200
    assert "event: decision" in stream.text
    assert f"id: {report.event_id}" in stream.text
    assert "FUTURE_REASON" in stream.text
    assert "event: health" in stream.text
    assert health.event_id in stream.text
    assert "FUTURE_HEALTH_REASON" in stream.text


def test_overview_returns_permission_scoped_real_sections(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    settings = settings_for(engine)
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
