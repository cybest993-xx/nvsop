"""真实 PostgreSQL 中模板绑定/报告父对象删除保护的证据。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.orm import Session as DatabaseSession
from template_fixtures import add_template_version

from factory_sop.device.adapters.command_repository import PostgresPendingCommandRepository
from factory_sop.device.adapters.repository import (
    PostgresInferenceBackendRepository,
    PostgresInferenceHostRepository,
    PostgresStationRepository,
)
from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.model import (
    ConnectionState,
    DeviceStatus,
    InferenceBackend,
    InferenceHost,
    PendingCommand,
    PendingCommandStatus,
    PendingCommandType,
    Station,
)
from factory_sop.identifiers import new_id
from factory_sop.template.adapters.repository import PostgresTemplateRepository
from factory_sop.template.model import TemplateConfigurationReport, TemplateStationBinding

NOW = datetime(2026, 9, 9, 1, 0, tzinfo=UTC)


def a_station() -> Station:
    actor = new_id()
    return Station(
        id=new_id(),
        code=f"history-{actor.hex[:12]}",
        name="模板历史测试工位",
        tags=(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor,
        updated_by=actor,
        created_at=NOW,
        updated_at=NOW,
    )


def a_host(*, name: str = "模板历史推理机") -> InferenceHost:
    actor = new_id()
    return InferenceHost(
        id=new_id(),
        name=f"{name}-{actor.hex[:8]}",
        address="10.0.9.11",
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


def a_backend(host_id: UUID) -> InferenceBackend:
    actor = new_id()
    return InferenceBackend(
        id=new_id(),
        host_id=host_id,
        base_url=f"http://10.0.9.11:{8000 + int(actor.hex[-3:], 16) % 1000}",
        template_version_id=None,
        status=DeviceStatus.ACTIVE,
        connection_state=ConnectionState.UNVERIFIED,
        connection_checked_at=None,
        connection_detail=None,
        self_reported_model_ids=(),
        self_reported_at=None,
        revision=1,
        created_by=actor,
        updated_by=actor,
        created_at=NOW,
        updated_at=NOW,
    )


def a_report(station_id: UUID, backend_id: UUID, host_id: UUID) -> TemplateConfigurationReport:
    return TemplateConfigurationReport(
        station_id=station_id,
        backend_id=backend_id,
        host_id=host_id,
        reported_version_id=None,
        reported_sha256=None,
        reported_config_revision=None,
        reported_at=None,
        last_rejection_code=None,
        last_rejection_detail=None,
        last_rejection_at=None,
        created_by=host_id,
        updated_by=host_id,
        created_at=NOW,
        updated_at=NOW,
    )


def test_station_delete_maps_a_template_binding_foreign_key(
    session: DatabaseSession,
) -> None:
    station = a_station()
    stations = PostgresStationRepository(session)
    stations.add(station)
    session.flush()
    # The binding FK itself is the only child of the target station here; the immutable
    # version belongs to the fixture's separate station so the older template FK cannot win
    # the database's delete check first.
    fixture = add_template_version(session, now=NOW)
    version = PostgresTemplateRepository(session).version_by_id(fixture.version_id)
    assert version is not None
    binding = TemplateStationBinding(
        id=new_id(),
        station_id=station.id,
        desired_version_id=version.id,
        desired_sha256=version.sha256,
        desired_config_revision=1,
        revision=1,
        created_by=version.published_by,
        updated_by=version.published_by,
        created_at=NOW,
        updated_at=NOW,
    )
    PostgresTemplateRepository(session).save_binding(binding)

    with pytest.raises(DeviceRefusedError) as refused:
        stations.remove(station.id, expected_revision=station.revision)

    assert refused.value.code is DeviceRefusalCode.STATION_HAS_TEMPLATE_BINDING


def test_station_delete_maps_a_template_configuration_report_foreign_key(
    session: DatabaseSession,
) -> None:
    station = a_station()
    host = a_host()
    backend = a_backend(host.id)
    stations = PostgresStationRepository(session)
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    stations.add(station)
    hosts.add(host)
    backends.add(backend)
    PostgresTemplateRepository(session).save_report(a_report(station.id, backend.id, host.id))

    with pytest.raises(DeviceRefusedError) as refused:
        stations.remove(station.id, expected_revision=station.revision)

    assert refused.value.code is DeviceRefusalCode.STATION_HAS_CONFIGURATION_REPORT


def test_backend_delete_maps_a_template_configuration_report_foreign_key(
    session: DatabaseSession,
) -> None:
    station = a_station()
    host = a_host()
    backend = a_backend(host.id)
    stations = PostgresStationRepository(session)
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    stations.add(station)
    hosts.add(host)
    backends.add(backend)
    PostgresTemplateRepository(session).save_report(a_report(station.id, backend.id, host.id))

    with pytest.raises(DeviceRefusedError) as refused:
        backends.remove(backend.id, expected_revision=backend.revision)

    assert refused.value.code is DeviceRefusalCode.INFERENCE_BACKEND_HAS_CONFIGURATION_REPORT


def test_host_delete_maps_a_template_configuration_report_foreign_key(
    session: DatabaseSession,
) -> None:
    station = a_station()
    reported_host = a_host(name="报告归属推理机")
    backend_host = a_host(name="后端归属推理机")
    backend = a_backend(backend_host.id)
    stations = PostgresStationRepository(session)
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    stations.add(station)
    hosts.add(reported_host)
    hosts.add(backend_host)
    backends.add(backend)
    # The report schema protects each parent independently. Using a backend on another host
    # isolates the host_id foreign key so the delete is not stopped first by host→backend.
    PostgresTemplateRepository(session).save_report(
        a_report(station.id, backend.id, reported_host.id)
    )

    with pytest.raises(DeviceRefusedError) as refused:
        hosts.remove(reported_host.id, expected_revision=reported_host.revision)

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_HAS_CONFIGURATION_REPORT


def test_host_delete_maps_a_pending_command_foreign_key(
    session: DatabaseSession,
) -> None:
    host = a_host(name="待处理命令主机")
    hosts = PostgresInferenceHostRepository(session)
    hosts.add(host)
    command = PendingCommand(
        id=new_id(),
        host_id=host.id,
        command_type=PendingCommandType.TEST_CONNECTOR_CONNECTION,
        target_id=new_id(),
        target_revision=1,
        idempotency_key=f"history-command-{host.id}",
        status=PendingCommandStatus.PENDING,
        attempt=0,
        claim_token=None,
        claimed_at=None,
        lease_expires_at=None,
        result=None,
        result_detail=None,
        failure_code=None,
        completed_at=None,
        created_by=host.created_by,
        created_at=NOW,
        updated_at=NOW,
    )
    PostgresPendingCommandRepository(session).add(command)

    with pytest.raises(DeviceRefusedError) as refused:
        hosts.remove(host.id, expected_revision=host.revision)

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_HAS_PENDING_COMMANDS
