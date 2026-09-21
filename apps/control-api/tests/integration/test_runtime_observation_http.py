"""真实 PostgreSQL/HTTP 验收：主机配置拉取、上报镜像、SSE 与概览。"""

from __future__ import annotations

import importlib
import json
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

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
    DECISION_REPORT_CONTRACT_VERSION,
    REPORT_CAPABILITIES_HEADER,
    SOP_INSTANCE_REPORT_CAPABILITY,
    SOP_INSTANCE_REPORT_CONTRACT_VERSION,
    ConfigurationBundle,
    HostIdentityKeyPair,
    HostIdentityRequest,
    ReportBackendProvenance,
    ReportedDecision,
    ReportedHealth,
    ReportedSopInstance,
    ReportEvidence,
    Unverified,
    configuration_from_wire,
    configuration_to_wire,
    generate_host_identity_key_pair,
    reported_decision_to_wire,
    reported_health_to_wire,
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


def test_edge_offline_decision_flushes_after_real_center_rebind(
    engine: Engine, runtime_topology: RuntimeTopology
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
    settings = settings_for(engine)
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
                    confirm_headers[REPORT_CAPABILITIES_HEADER] = SOP_INSTANCE_REPORT_CAPABILITY
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
                    instance_path = f"{API_PREFIX}/monitor/instances"
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
            attempts = edge_reporting.DecisionReporter(
                queues=edge_station, transport=transport
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
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    settings = settings_for(engine)
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
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    settings = settings_for(engine)
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
        station = bundle.stations[0]
        assert station.template is not None
        instance = ReportedSopInstance(
            event_id=f"{runtime_topology.host.id}:{runtime_topology.station.id}:instance:7",
            trace_id="trace-instance-integration-7",
            host_id=str(runtime_topology.host.id),
            station_id=str(runtime_topology.station.id),
            instance_id=7,
            opened_at=1.0,
            closed_at=None,
            close_reason=None,
            open_boundary_signal="fixture-start",
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
        instance_path = f"{API_PREFIX}/monitor/instances"
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
            close_boundary_signal="fixture-end-b",
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
            close_boundary_signal="fixture-end-b",
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
        instance_list = client.get(instance_path)
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
    assert instance_list.json()["items"][0] == closed_instance_body
    assert instance_list.json()["items"][0]["open_boundary_signal"] == "fixture-start"
    assert instance_list.json()["items"][0]["close_boundary_signal"] == "fixture-end-b"
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
