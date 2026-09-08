"""委托测试命令在中心用例接缝上的幂等行为。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from auth_fakes import caller_holding
from device_fakes import FakeConnectors, FakeInferenceHosts, FakePendingCommands

from factory_sop.auth.api import Permission
from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.model import (
    ConnectorReachability,
    ConnectorTestResult,
    InferenceHostIdentity,
    PendingCommand,
    PendingCommandStatus,
)
from factory_sop.device.usecases.commands import (
    claim_next_command,
    complete_connection_test,
    enqueue_connector_connection_test,
)

NOW = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)
CALLER = caller_holding(Permission.CONNECTOR_EDIT)


def host_identity(hosts: FakeInferenceHosts, host_id: UUID) -> InferenceHostIdentity:
    return InferenceHostIdentity(host_id=host_id, credential=hosts.credential_for(host_id))


InMemoryPendingCommands = FakePendingCommands


class ConcurrentIdempotencyPendingCommands(FakePendingCommands):
    """模拟两个事务同时未读到幂等键的仓储接缝。"""

    reads: int = 0

    def by_idempotency_key(self, key: str) -> PendingCommand | None:
        self.reads += 1
        if self.reads <= 2:
            return None
        return super().by_idempotency_key(key)

    def add(self, command: PendingCommand) -> None:
        if super().by_idempotency_key(command.idempotency_key) is None:
            self.rows[command.id] = command

    def add_or_get(self, command: PendingCommand) -> PendingCommand:
        existing = super().by_idempotency_key(command.idempotency_key)
        if existing is not None:
            return existing
        self.rows[command.id] = command
        return command


def test_same_idempotency_key_returns_one_persisted_command_without_changing_connector_state() -> (
    None
):
    hosts = FakeInferenceHosts()
    connectors = FakeConnectors()
    host = hosts.register(name="装配A线-推理机1")
    connector = connectors.register(station_id=UUID(int=1), host_id=host.id)
    commands = InMemoryPendingCommands()

    first = enqueue_connector_connection_test(
        connector_id=connector.id,
        idempotency_key="connection-test-1",
        caller=CALLER,
        now=NOW,
        connectors=connectors,
        hosts=hosts,
        commands=commands,
    )
    repeated = enqueue_connector_connection_test(
        connector_id=connector.id,
        idempotency_key="connection-test-1",
        caller=CALLER,
        now=NOW,
        connectors=connectors,
        hosts=hosts,
        commands=commands,
    )

    assert repeated == first
    assert len(commands.rows) == 1
    assert repeated.host_id == host.id
    assert repeated.target_id == connector.id
    assert repeated.target_revision == connector.revision
    assert not hasattr(repeated, "password")
    assert connectors.by_id(connector.id) == connector


def test_concurrent_same_idempotency_key_returns_the_winning_command() -> None:
    hosts = FakeInferenceHosts()
    connectors = FakeConnectors()
    host = hosts.register(name="装配A线-推理机1")
    connector = connectors.register(station_id=UUID(int=1), host_id=host.id)
    commands = ConcurrentIdempotencyPendingCommands()

    first = enqueue_connector_connection_test(
        connector_id=connector.id,
        idempotency_key="connection-test-concurrent",
        caller=CALLER,
        now=NOW,
        connectors=connectors,
        hosts=hosts,
        commands=commands,
    )
    second = enqueue_connector_connection_test(
        connector_id=connector.id,
        idempotency_key="connection-test-concurrent",
        caller=CALLER,
        now=NOW,
        connectors=connectors,
        hosts=hosts,
        commands=commands,
    )

    assert second == first
    assert list(commands.rows.values()) == [first]


def test_claim_and_real_result_complete_the_command_and_update_connector_state() -> None:
    hosts = FakeInferenceHosts()
    connectors = FakeConnectors()
    host = hosts.register(name="装配A线-推理机1")
    connector = connectors.register(station_id=UUID(int=1), host_id=host.id)
    commands = InMemoryPendingCommands()
    pending = enqueue_connector_connection_test(
        connector_id=connector.id,
        idempotency_key="connection-test-2",
        caller=CALLER,
        now=NOW,
        connectors=connectors,
        hosts=hosts,
        commands=commands,
    )

    claimed = claim_next_command(
        host=host_identity(hosts, host.id),
        now=NOW,
        lease_duration=timedelta(minutes=1),
        hosts=hosts,
        commands=commands,
    )
    assert claimed is not None
    assert claimed.id == pending.id
    assert claimed.status is PendingCommandStatus.CLAIMED
    assert claimed.claim_token is not None

    completed = complete_connection_test(
        command_id=claimed.id,
        host=host_identity(hosts, host.id),
        claim_token=claimed.claim_token,
        result=ConnectorTestResult(
            reachability=ConnectorReachability.REACHABLE,
            credentials_configured=True,
        ),
        now=NOW + timedelta(seconds=5),
        connectors=connectors,
        hosts=hosts,
        commands=commands,
    )

    assert completed.status is PendingCommandStatus.SUCCEEDED
    assert completed.result is ConnectorReachability.REACHABLE
    assert connectors.by_id(connector.id) is not None
    updated = connectors.by_id(connector.id)
    assert updated is not None
    assert updated.reachability is ConnectorReachability.REACHABLE
    assert updated.credentials_configured is True
    assert updated.revision == connector.revision


def test_health_result_keeps_configuration_revision_for_the_next_command() -> None:
    hosts = FakeInferenceHosts()
    connectors = FakeConnectors()
    host = hosts.register(name="装配A线-推理机1")
    connector = connectors.register(station_id=UUID(int=1), host_id=host.id)
    commands = InMemoryPendingCommands()
    pending = enqueue_connector_connection_test(
        connector_id=connector.id,
        idempotency_key="connection-test-revision-stable-1",
        caller=CALLER,
        now=NOW,
        connectors=connectors,
        hosts=hosts,
        commands=commands,
    )
    claimed = claim_next_command(
        host=host_identity(hosts, host.id),
        now=NOW,
        lease_duration=timedelta(minutes=1),
        hosts=hosts,
        commands=commands,
    )
    assert claimed is not None
    assert claimed.claim_token is not None

    completed = complete_connection_test(
        command_id=pending.id,
        host=host_identity(hosts, host.id),
        claim_token=claimed.claim_token,
        result=ConnectorTestResult(
            reachability=ConnectorReachability.REACHABLE,
            credentials_configured=True,
        ),
        now=NOW + timedelta(seconds=1),
        connectors=connectors,
        hosts=hosts,
        commands=commands,
    )

    assert completed.status is PendingCommandStatus.SUCCEEDED
    updated = connectors.by_id(connector.id)
    assert updated is not None
    assert updated.revision == connector.revision

    next_pending = enqueue_connector_connection_test(
        connector_id=connector.id,
        idempotency_key="connection-test-revision-stable-2",
        caller=CALLER,
        now=NOW + timedelta(seconds=2),
        connectors=connectors,
        hosts=hosts,
        commands=commands,
    )
    assert next_pending.target_revision == connector.revision


def test_another_host_cannot_claim_or_complete_the_command() -> None:
    hosts = FakeInferenceHosts()
    connectors = FakeConnectors()
    owner = hosts.register(name="装配A线-推理机1")
    other = hosts.register(name="装配A线-推理机2")
    connector = connectors.register(station_id=UUID(int=1), host_id=owner.id)
    commands = InMemoryPendingCommands()
    pending = enqueue_connector_connection_test(
        connector_id=connector.id,
        idempotency_key="connection-test-isolated",
        caller=CALLER,
        now=NOW,
        connectors=connectors,
        hosts=hosts,
        commands=commands,
    )

    assert (
        claim_next_command(
            host=host_identity(hosts, other.id),
            now=NOW,
            lease_duration=timedelta(minutes=1),
            hosts=hosts,
            commands=commands,
        )
        is None
    )
    claimed = claim_next_command(
        host=host_identity(hosts, owner.id),
        now=NOW,
        lease_duration=timedelta(minutes=1),
        hosts=hosts,
        commands=commands,
    )
    assert claimed is not None
    assert claimed.id == pending.id
    assert claimed.claim_token is not None

    with pytest.raises(DeviceRefusedError) as refused:
        complete_connection_test(
            command_id=claimed.id,
            host=host_identity(hosts, other.id),
            claim_token=claimed.claim_token,
            result=ConnectorTestResult(ConnectorReachability.REACHABLE),
            now=NOW + timedelta(seconds=1),
            connectors=connectors,
            hosts=hosts,
            commands=commands,
        )
    assert refused.value.code is DeviceRefusalCode.COMMAND_HOST_MISMATCH
    assert connectors.by_id(connector.id) == connector


def test_a_late_result_after_the_claim_lease_expires_does_not_change_state() -> None:
    hosts = FakeInferenceHosts()
    connectors = FakeConnectors()
    host = hosts.register(name="装配A线-推理机1")
    connector = connectors.register(station_id=UUID(int=1), host_id=host.id)
    commands = InMemoryPendingCommands()
    enqueue_connector_connection_test(
        connector_id=connector.id,
        idempotency_key="connection-test-expired",
        caller=CALLER,
        now=NOW,
        connectors=connectors,
        hosts=hosts,
        commands=commands,
    )
    claimed = claim_next_command(
        host=host_identity(hosts, host.id),
        now=NOW,
        lease_duration=timedelta(seconds=1),
        hosts=hosts,
        commands=commands,
    )
    assert claimed is not None
    assert claimed.claim_token is not None

    with pytest.raises(DeviceRefusedError) as refused:
        complete_connection_test(
            command_id=claimed.id,
            host=host_identity(hosts, host.id),
            claim_token=claimed.claim_token,
            result=ConnectorTestResult(ConnectorReachability.REACHABLE),
            now=NOW + timedelta(seconds=2),
            connectors=connectors,
            hosts=hosts,
            commands=commands,
        )
    assert refused.value.code is DeviceRefusalCode.COMMAND_CLAIM_EXPIRED
    assert commands.by_id(claimed.id) == claimed
    assert connectors.by_id(connector.id) == connector


def test_a_result_for_an_old_connector_revision_is_rejected_without_overwriting_new_config() -> (
    None
):
    hosts = FakeInferenceHosts()
    connectors = FakeConnectors()
    host = hosts.register(name="装配A线-推理机1")
    connector = connectors.register(station_id=UUID(int=1), host_id=host.id)
    commands = InMemoryPendingCommands()
    enqueue_connector_connection_test(
        connector_id=connector.id,
        idempotency_key="connection-test-revision",
        caller=CALLER,
        now=NOW,
        connectors=connectors,
        hosts=hosts,
        commands=commands,
    )
    claimed = claim_next_command(
        host=host_identity(hosts, host.id),
        now=NOW,
        lease_duration=timedelta(minutes=1),
        hosts=hosts,
        commands=commands,
    )
    assert claimed is not None
    assert claimed.claim_token is not None
    changed = replace(
        connector,
        name="已改名连接器",
        revision=connector.revision + 1,
        updated_at=NOW + timedelta(seconds=1),
    )
    connectors.save(changed, expected_revision=connector.revision)

    rejected = complete_connection_test(
        command_id=claimed.id,
        host=host_identity(hosts, host.id),
        claim_token=claimed.claim_token,
        result=ConnectorTestResult(ConnectorReachability.REACHABLE),
        now=NOW + timedelta(seconds=2),
        connectors=connectors,
        hosts=hosts,
        commands=commands,
    )

    assert rejected.status is PendingCommandStatus.REJECTED
    assert rejected.failure_code == "COMMAND_CONFIGURATION_CHANGED"
    assert connectors.by_id(connector.id) == changed


def test_repeating_the_same_result_is_idempotent_after_completion() -> None:
    hosts = FakeInferenceHosts()
    connectors = FakeConnectors()
    host = hosts.register(name="装配A线-推理机1")
    connector = connectors.register(station_id=UUID(int=1), host_id=host.id)
    commands = InMemoryPendingCommands()
    enqueue_connector_connection_test(
        connector_id=connector.id,
        idempotency_key="connection-test-repeat",
        caller=CALLER,
        now=NOW,
        connectors=connectors,
        hosts=hosts,
        commands=commands,
    )
    claimed = claim_next_command(
        host=host_identity(hosts, host.id),
        now=NOW,
        lease_duration=timedelta(minutes=1),
        hosts=hosts,
        commands=commands,
    )
    assert claimed is not None
    assert claimed.claim_token is not None
    result = ConnectorTestResult(ConnectorReachability.REACHABLE)
    completed = complete_connection_test(
        command_id=claimed.id,
        host=host_identity(hosts, host.id),
        claim_token=claimed.claim_token,
        result=result,
        now=NOW + timedelta(seconds=1),
        connectors=connectors,
        hosts=hosts,
        commands=commands,
    )
    current = connectors.by_id(connector.id)
    assert current is not None

    repeated = complete_connection_test(
        command_id=claimed.id,
        host=host_identity(hosts, host.id),
        claim_token=claimed.claim_token,
        result=result,
        now=NOW + timedelta(seconds=2),
        connectors=connectors,
        hosts=hosts,
        commands=commands,
    )

    assert repeated == completed
    assert connectors.by_id(connector.id) == current


def test_local_rejection_completes_without_overwriting_connector_health() -> None:
    hosts = FakeInferenceHosts()
    connectors = FakeConnectors()
    host = hosts.register(name="装配A线-推理机1")
    connector = connectors.register(station_id=UUID(int=1), host_id=host.id)
    commands = InMemoryPendingCommands()
    pending = enqueue_connector_connection_test(
        connector_id=connector.id,
        idempotency_key="connection-test-local-rejection",
        caller=CALLER,
        now=NOW,
        connectors=connectors,
        hosts=hosts,
        commands=commands,
    )
    claimed = claim_next_command(
        host=host_identity(hosts, host.id),
        now=NOW,
        lease_duration=timedelta(minutes=1),
        hosts=hosts,
        commands=commands,
    )
    assert claimed is not None
    assert claimed.claim_token is not None

    rejected = complete_connection_test(
        command_id=pending.id,
        host=host_identity(hosts, host.id),
        claim_token=claimed.claim_token,
        result=ConnectorTestResult(
            reachability=None,
            detail="推理机未配置该连接器凭据",
            credentials_configured=False,
            failure_code="COMMAND_CREDENTIALS_NOT_CONFIGURED",
        ),
        now=NOW + timedelta(seconds=1),
        connectors=connectors,
        hosts=hosts,
        commands=commands,
    )

    assert rejected.status is PendingCommandStatus.REJECTED
    assert rejected.result is None
    assert rejected.result_detail == "推理机未配置该连接器凭据"
    assert rejected.failure_code == "COMMAND_CREDENTIALS_NOT_CONFIGURED"
    assert connectors.by_id(connector.id) == connector


if __name__ == "__main__":
    raise SystemExit("pytest runs this module")
