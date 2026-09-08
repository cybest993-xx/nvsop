"""委托命令仓储的 PostgreSQL 持久化接缝。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.auth.api import Caller, Permission
from factory_sop.auth.model import User, UserStatus
from factory_sop.device.adapters.command_repository import PostgresPendingCommandRepository
from factory_sop.device.adapters.repository import (
    PostgresConnectorRepository,
    PostgresInferenceHostRepository,
    PostgresStationRepository,
)
from factory_sop.device.host_credentials import fingerprint
from factory_sop.device.model import (
    Connector,
    ConnectorConfiguration,
    ConnectorReachability,
    ConnectorTestResult,
    ConnectorType,
    DeviceStatus,
    InferenceHost,
    InferenceHostIdentity,
    PendingCommand,
    PendingCommandStatus,
    PendingCommandType,
    Station,
)
from factory_sop.device.usecases.commands import (
    claim_next_command,
    complete_connection_test,
    enqueue_connector_connection_test,
)
from factory_sop.identifiers import new_id
from nvsop_contracts import Unverified

NOW = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)
HOST_A_TOKEN = "integration-host-token-a"  # pragma: allowlist secret
HOST_B_TOKEN = "integration-host-token-b"  # pragma: allowlist secret


def test_a_committed_command_is_read_back_by_a_new_session(engine: Engine) -> None:
    actor_id = new_id()
    actor = Caller(
        user=User(
            id=actor_id,
            login_name="device-admin",
            display_name="设备管理员",
            password_hash="not-used",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
        granted=frozenset({Permission.CONNECTOR_EDIT}),
    )
    host = InferenceHost(
        id=new_id(),
        name="推理机-命令持久化",
        address="10.0.8.11",
        mediamtx_address=None,
        recording_window_seconds=604800,
        disk_watermark_percent=85,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor_id,
        updated_by=actor_id,
        created_at=NOW,
        updated_at=NOW,
    )
    station = Station(
        id=new_id(),
        code="CMD-001",
        name="命令测试工位",
        tags=(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor_id,
        updated_by=actor_id,
        created_at=NOW,
        updated_at=NOW,
    )
    connector = Connector(
        id=new_id(),
        station_id=station.id,
        host_id=host.id,
        name="命令测试连接器",
        connector_type=ConnectorType.HIKVISION_ISAPI,
        configuration=ConnectorConfiguration(address="10.0.8.21"),
        credentials_configured=False,
        reachability=ConnectorReachability.UNVERIFIED,
        health_detail=None,
        capability=Unverified(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor_id,
        updated_by=actor_id,
        created_at=NOW,
        updated_at=NOW,
    )
    command_id: UUID | None = None

    try:
        with DatabaseSession(engine) as writing:
            PostgresInferenceHostRepository(writing).add(host)
            PostgresStationRepository(writing).add(station)
            PostgresConnectorRepository(writing).add(connector)
            command = enqueue_connector_connection_test(
                connector_id=connector.id,
                idempotency_key="postgres-command-1",
                caller=actor,
                now=NOW,
                connectors=PostgresConnectorRepository(writing),
                hosts=PostgresInferenceHostRepository(writing),
                commands=PostgresPendingCommandRepository(writing),
            )
            command_id = command.id
            writing.commit()

        with DatabaseSession(engine) as reading:
            loaded = PostgresPendingCommandRepository(reading).by_id(command.id)

        assert loaded == command
    finally:
        with engine.begin() as cleanup:
            if command_id is not None:
                cleanup.execute(
                    text("DELETE FROM device_pending_command WHERE id = :id"),
                    {"id": command_id},
                )
            cleanup.execute(
                text("DELETE FROM device_connector WHERE id = :id"),
                {"id": connector.id},
            )
            cleanup.execute(
                text("DELETE FROM device_station WHERE id = :id"),
                {"id": station.id},
            )
            cleanup.execute(
                text("DELETE FROM device_inference_host WHERE id = :id"),
                {"id": host.id},
            )


def test_concurrent_idempotent_inserts_return_the_same_postgres_command(engine: Engine) -> None:
    actor_id = new_id()
    host = InferenceHost(
        id=new_id(),
        name="推理机-并发幂等",
        address="10.0.8.14",
        mediamtx_address=None,
        recording_window_seconds=604800,
        disk_watermark_percent=85,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor_id,
        updated_by=actor_id,
        created_at=NOW,
        updated_at=NOW,
    )
    command_a = PendingCommand(
        id=new_id(),
        host_id=host.id,
        command_type=PendingCommandType.TEST_CONNECTOR_CONNECTION,
        target_id=new_id(),
        target_revision=1,
        idempotency_key="postgres-command-concurrent",
        status=PendingCommandStatus.PENDING,
        attempt=0,
        claim_token=None,
        claimed_at=None,
        lease_expires_at=None,
        result=None,
        result_detail=None,
        failure_code=None,
        completed_at=None,
        created_by=actor_id,
        created_at=NOW,
        updated_at=NOW,
    )
    command_b = PendingCommand(
        id=new_id(),
        host_id=host.id,
        command_type=PendingCommandType.TEST_CONNECTOR_CONNECTION,
        target_id=command_a.target_id,
        target_revision=1,
        idempotency_key=command_a.idempotency_key,
        status=PendingCommandStatus.PENDING,
        attempt=0,
        claim_token=None,
        claimed_at=None,
        lease_expires_at=None,
        result=None,
        result_detail=None,
        failure_code=None,
        completed_at=None,
        created_by=actor_id,
        created_at=NOW,
        updated_at=NOW,
    )

    try:
        with DatabaseSession(engine) as setup:
            PostgresInferenceHostRepository(setup).add(host)
            setup.commit()

        barrier = Barrier(2)

        def insert(command: PendingCommand) -> PendingCommand:
            with DatabaseSession(engine) as session:
                barrier.wait()
                stored = PostgresPendingCommandRepository(session).add_or_get(command)
                session.commit()
                return stored

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(insert, (command_a, command_b)))

        assert results[0] == results[1]
        assert results[0].id in {command_a.id, command_b.id}
    finally:
        with engine.begin() as cleanup:
            cleanup.execute(
                text("DELETE FROM device_pending_command WHERE idempotency_key = :idempotency_key"),
                {"idempotency_key": command_a.idempotency_key},
            )
            cleanup.execute(
                text("DELETE FROM device_inference_host WHERE id = :id"),
                {"id": host.id},
            )


def test_claim_is_host_scoped_reclaimed_after_expiry_and_persists_rejection(
    engine: Engine,
) -> None:
    actor_id = new_id()
    caller = Caller(
        user=User(
            id=actor_id,
            login_name="command-operator",
            display_name="命令操作员",
            password_hash="not-used",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
        granted=frozenset({Permission.CONNECTOR_EDIT}),
    )
    host_a = InferenceHost(
        id=new_id(),
        name="推理机-隔离-A",
        address="10.0.8.12",
        mediamtx_address=None,
        recording_window_seconds=604800,
        disk_watermark_percent=85,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor_id,
        updated_by=actor_id,
        created_at=NOW,
        updated_at=NOW,
        credential_hash=fingerprint(credential=HOST_A_TOKEN),
    )
    host_b = InferenceHost(
        id=new_id(),
        name="推理机-隔离-B",
        address="10.0.8.13",
        mediamtx_address=None,
        recording_window_seconds=604800,
        disk_watermark_percent=85,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor_id,
        updated_by=actor_id,
        created_at=NOW,
        updated_at=NOW,
        credential_hash=fingerprint(credential=HOST_B_TOKEN),
    )
    station = Station(
        id=new_id(),
        code="CMD-002",
        name="领取隔离工位",
        tags=(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor_id,
        updated_by=actor_id,
        created_at=NOW,
        updated_at=NOW,
    )
    connector = Connector(
        id=new_id(),
        station_id=station.id,
        host_id=host_a.id,
        name="隔离测试连接器",
        connector_type=ConnectorType.HIKVISION_ISAPI,
        configuration=ConnectorConfiguration(address="10.0.8.22"),
        credentials_configured=False,
        reachability=ConnectorReachability.UNVERIFIED,
        health_detail=None,
        capability=Unverified(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor_id,
        updated_by=actor_id,
        created_at=NOW,
        updated_at=NOW,
    )
    command_id: UUID | None = None

    try:
        with DatabaseSession(engine) as writing:
            host_store = PostgresInferenceHostRepository(writing)
            station_store = PostgresStationRepository(writing)
            connector_store = PostgresConnectorRepository(writing)
            command_store = PostgresPendingCommandRepository(writing)
            host_store.add(host_a)
            host_store.add(host_b)
            station_store.add(station)
            connector_store.add(connector)
            queued = enqueue_connector_connection_test(
                connector_id=connector.id,
                idempotency_key="postgres-command-isolation",
                caller=caller,
                now=NOW,
                connectors=connector_store,
                hosts=host_store,
                commands=command_store,
            )
            command_id = queued.id
            writing.commit()

        with DatabaseSession(engine) as claiming:
            claimed = claim_next_command(
                host=InferenceHostIdentity(host_id=host_a.id, credential=HOST_A_TOKEN),
                now=NOW,
                lease_duration=timedelta(seconds=1),
                hosts=PostgresInferenceHostRepository(claiming),
                commands=PostgresPendingCommandRepository(claiming),
            )
            assert claimed is not None
            first_token = claimed.claim_token
            assert first_token is not None
            claiming.commit()

        with DatabaseSession(engine) as other_host:
            assert (
                claim_next_command(
                    host=InferenceHostIdentity(host_id=host_b.id, credential=HOST_B_TOKEN),
                    now=NOW + timedelta(milliseconds=100),
                    lease_duration=timedelta(minutes=1),
                    hosts=PostgresInferenceHostRepository(other_host),
                    commands=PostgresPendingCommandRepository(other_host),
                )
                is None
            )
            other_host.commit()

        with DatabaseSession(engine) as before_expiry:
            assert (
                claim_next_command(
                    host=InferenceHostIdentity(host_id=host_a.id, credential=HOST_A_TOKEN),
                    now=NOW + timedelta(milliseconds=500),
                    lease_duration=timedelta(minutes=1),
                    hosts=PostgresInferenceHostRepository(before_expiry),
                    commands=PostgresPendingCommandRepository(before_expiry),
                )
                is None
            )
            before_expiry.commit()

        with DatabaseSession(engine) as recovered:
            retried = claim_next_command(
                host=InferenceHostIdentity(host_id=host_a.id, credential=HOST_A_TOKEN),
                now=NOW + timedelta(seconds=2),
                lease_duration=timedelta(minutes=1),
                hosts=PostgresInferenceHostRepository(recovered),
                commands=PostgresPendingCommandRepository(recovered),
            )
            assert retried is not None
            assert retried.id == queued.id
            assert retried.attempt == 2
            assert retried.claim_token is not None
            assert retried.claim_token != first_token
            rejected = complete_connection_test(
                command_id=retried.id,
                host=InferenceHostIdentity(host_id=host_a.id, credential=HOST_A_TOKEN),
                claim_token=retried.claim_token,
                result=ConnectorTestResult(
                    reachability=None,
                    detail="推理机未配置该连接器凭据",
                    credentials_configured=False,
                    failure_code="COMMAND_CREDENTIALS_NOT_CONFIGURED",
                ),
                now=NOW + timedelta(seconds=2, milliseconds=100),
                connectors=PostgresConnectorRepository(recovered),
                hosts=PostgresInferenceHostRepository(recovered),
                commands=PostgresPendingCommandRepository(recovered),
            )
            assert rejected.status is PendingCommandStatus.REJECTED
            recovered.commit()

        with DatabaseSession(engine) as reading:
            loaded = PostgresPendingCommandRepository(reading).by_id(queued.id)
            loaded_connector = PostgresConnectorRepository(reading).by_id(connector.id)
            assert loaded is not None
            assert loaded.status is PendingCommandStatus.REJECTED
            assert loaded.failure_code == "COMMAND_CREDENTIALS_NOT_CONFIGURED"
            assert loaded.result_detail == "推理机未配置该连接器凭据"
            assert loaded_connector == connector
    finally:
        with engine.begin() as cleanup:
            if command_id is not None:
                cleanup.execute(
                    text("DELETE FROM device_pending_command WHERE id = :id"),
                    {"id": command_id},
                )
            cleanup.execute(
                text("DELETE FROM device_connector WHERE id = :id"),
                {"id": connector.id},
            )
            cleanup.execute(
                text("DELETE FROM device_station WHERE id = :id"),
                {"id": station.id},
            )
            cleanup.execute(
                text("DELETE FROM device_inference_host WHERE id IN (:host_a, :host_b)"),
                {"host_a": host_a.id, "host_b": host_b.id},
            )
