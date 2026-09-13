"""中心委托命令的业务接缝：入队、领取、回报和重试。"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.device.errors import DeviceRefusalCode
from factory_sop.device.model import (
    Connector,
    ConnectorReachability,
    ConnectorTestResult,
    DeviceStatus,
    InferenceHostIdentity,
    PendingCommand,
    PendingCommandCompletion,
    PendingCommandStatus,
    PendingCommandType,
)
from factory_sop.device.repository import (
    ConnectorRepository,
    InferenceHostRepository,
    PendingCommandRepository,
)
from factory_sop.device.usecases._transitions import refuse
from factory_sop.identifiers import new_id
from factory_sop.observability import get_logger
from nvsop_contracts import verify_host_identity_request

_logger = get_logger("device")
_REFUSAL_EVENT = "device.pending_command.refused"
_INFERENCE_HOST_SIGNATURE_FRESHNESS = timedelta(seconds=300)


def enqueue_connector_connection_test(
    *,
    connector_id: UUID,
    idempotency_key: str,
    caller: Caller,
    now: datetime,
    connectors: ConnectorRepository,
    hosts: InferenceHostRepository,
    commands: PendingCommandRepository,
) -> PendingCommand:
    """为连接器创建一个不含凭据的持久测试命令。

    重复幂等键返回原命令；新命令快照连接器修订号，结果只能回写到同一配置版本。
    """
    authorize(caller, Permission.CONNECTOR_EDIT)
    existing = commands.by_idempotency_key(idempotency_key)
    if existing is not None:
        if (
            existing.command_type is not PendingCommandType.TEST_CONNECTOR_CONNECTION
            or existing.target_id != connector_id
        ):
            refuse(
                _REFUSAL_EVENT,
                DeviceRefusalCode.COMMAND_IDEMPOTENCY_CONFLICT,
                idempotency_key=idempotency_key,
                actor_id=str(caller.user.id),
            )
        return existing

    connector = _existing_connector(connector_id, connectors)
    if connector.status is DeviceStatus.DEACTIVATED:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.CONNECTOR_DEACTIVATED,
            connector_id=str(connector_id),
            actor_id=str(caller.user.id),
        )
    host = hosts.by_id(connector.host_id)
    if host is None:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND,
            host_id=str(connector.host_id),
            actor_id=str(caller.user.id),
        )
    if host.status is DeviceStatus.DEACTIVATED:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED,
            host_id=str(host.id),
            actor_id=str(caller.user.id),
        )

    command = PendingCommand(
        id=new_id(),
        host_id=host.id,
        command_type=PendingCommandType.TEST_CONNECTOR_CONNECTION,
        target_id=connector.id,
        target_revision=connector.revision,
        idempotency_key=idempotency_key,
        status=PendingCommandStatus.PENDING,
        attempt=0,
        claim_token=None,
        claimed_at=None,
        lease_expires_at=None,
        result=None,
        result_detail=None,
        failure_code=None,
        completed_at=None,
        created_by=caller.user.id,
        created_at=now,
        updated_at=now,
    )
    stored = commands.add_or_get(command)
    if (
        stored.command_type is not PendingCommandType.TEST_CONNECTOR_CONNECTION
        or stored.target_id != connector_id
    ):
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.COMMAND_IDEMPOTENCY_CONFLICT,
            idempotency_key=idempotency_key,
            actor_id=str(caller.user.id),
        )
    if stored.id == command.id:
        _logger.info(
            "device.pending_command.created",
            command_id=str(command.id),
            command_type=command.command_type.value,
            host_id=str(command.host_id),
            target_id=str(command.target_id),
            target_revision=str(command.target_revision),
            actor_id=str(caller.user.id),
        )
    return stored


def claim_next_command(
    *,
    host: InferenceHostIdentity,
    now: datetime,
    lease_duration: timedelta,
    hosts: InferenceHostRepository,
    commands: PendingCommandRepository,
) -> PendingCommand | None:
    """认证后原子领取该推理机的下一条命令。"""
    authenticate_command_host(host=host, now=now, hosts=hosts)
    if lease_duration <= timedelta(0):
        raise ValueError("command lease duration must be positive")
    token = str(new_id())
    claimed = commands.claim_next(
        host_id=host.host_id,
        claim_token=token,
        claimed_at=now,
        lease_expires_at=now + lease_duration,
    )
    if claimed is not None:
        _logger.info(
            "device.pending_command.claimed",
            command_id=str(claimed.id),
            host_id=str(host.host_id),
            attempt=str(claimed.attempt),
        )
    return claimed


def authenticate_command_host(
    *, host: InferenceHostIdentity, now: datetime, hosts: InferenceHostRepository
) -> None:
    """用登记的非秘密公钥验证主机请求，失败时不暴露主机是否存在。"""
    stored = hosts.by_id(host.host_id)
    request = host.request
    fresh = (
        request is not None
        and request.host_id == str(host.host_id)
        and abs(int(now.timestamp()) - request.timestamp)
        <= _INFERENCE_HOST_SIGNATURE_FRESHNESS.total_seconds()
    )
    public_key = stored.identity_public_key if stored is not None else None
    authenticated = bool(
        public_key
        and fresh
        and request is not None
        and verify_host_identity_request(
            request,
            public_key=public_key,
            signature=host.signature or "",
        )
    )
    if not authenticated:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.INFERENCE_HOST_AUTHENTICATION_FAILED,
            host_id=str(host.host_id),
        )
    assert stored is not None
    assert request is not None
    if not hosts.consume_identity_nonce(
        host_id=host.host_id,
        nonce=request.nonce,
        seen_at=now,
    ):
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.INFERENCE_HOST_AUTHENTICATION_FAILED,
            host_id=str(host.host_id),
        )
    if stored.status is DeviceStatus.DEACTIVATED:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED,
            host_id=str(host.host_id),
        )


def pending_command_by_identifier(
    *,
    command_id: UUID,
    caller: Caller,
    commands: PendingCommandRepository,
) -> PendingCommand:
    """读取操作员可见的命令状态; 只有连接器查看权限能看到结果。"""
    authorize(caller, Permission.CONNECTOR_VIEW)
    return _existing_command(command_id, commands)


def complete_connection_test(
    *,
    command_id: UUID,
    host: InferenceHostIdentity,
    claim_token: str,
    result: ConnectorTestResult,
    now: datetime,
    connectors: ConnectorRepository,
    hosts: InferenceHostRepository,
    commands: PendingCommandRepository,
    authenticated: bool = False,
) -> PendingCommand:
    """认证领取主机后记录连接器的真实测试结果。

    命令只允许写回创建时的连接器修订；配置变化、目标停用或目标消失均留下可见的拒绝
    结案，不会把迟到结果写进新配置。
    """
    if not authenticated:
        authenticate_command_host(host=host, now=now, hosts=hosts)
    host_id = host.host_id
    command = _existing_command_for_update(command_id, commands)
    if command.host_id != host_id:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.COMMAND_HOST_MISMATCH,
            command_id=str(command_id),
            host_id=str(host_id),
        )
    if command.status in {
        PendingCommandStatus.SUCCEEDED,
        PendingCommandStatus.FAILED,
        PendingCommandStatus.REJECTED,
    }:
        if command.claim_token == claim_token and _same_result(command, result):
            return command
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.COMMAND_ALREADY_COMPLETED,
            command_id=str(command_id),
            host_id=str(host_id),
        )
    if command.status is not PendingCommandStatus.CLAIMED:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.COMMAND_CLAIM_REQUIRED,
            command_id=str(command_id),
            host_id=str(host_id),
        )
    if command.claim_token != claim_token:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.COMMAND_CLAIM_TOKEN_INVALID,
            command_id=str(command_id),
            host_id=str(host_id),
        )
    if command.lease_expires_at is None or now >= command.lease_expires_at:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.COMMAND_CLAIM_EXPIRED,
            command_id=str(command_id),
            host_id=str(host_id),
        )
    reachability = result.reachability
    if reachability is None:
        return _reject(
            command,
            code=result.failure_code or "COMMAND_RESULT_INVALID",
            detail=result.detail,
            credentials_configured=result.credentials_configured,
            now=now,
            host_id=host_id,
            commands=commands,
        )

    connector = connectors.by_id(command.target_id)
    if connector is None:
        return _reject(
            command,
            code=DeviceRefusalCode.COMMAND_TARGET_NOT_FOUND,
            credentials_configured=result.credentials_configured,
            now=now,
            host_id=host_id,
            commands=commands,
        )
    if connector.status is DeviceStatus.DEACTIVATED:
        return _reject(
            command,
            code=DeviceRefusalCode.COMMAND_TARGET_DEACTIVATED,
            credentials_configured=result.credentials_configured,
            now=now,
            host_id=host_id,
            commands=commands,
        )
    if connector.revision != command.target_revision:
        return _reject(
            command,
            code=DeviceRefusalCode.COMMAND_CONFIGURATION_CHANGED,
            credentials_configured=result.credentials_configured,
            now=now,
            host_id=host_id,
            commands=commands,
        )

    updated_connector = Connector(
        id=connector.id,
        station_id=connector.station_id,
        host_id=connector.host_id,
        name=connector.name,
        connector_type=connector.connector_type,
        configuration=connector.configuration,
        credentials_configured=(
            connector.credentials_configured
            if result.credentials_configured is None
            else result.credentials_configured
        ),
        reachability=reachability,
        health_detail=result.detail,
        capability=connector.capability,
        status=connector.status,
        # 健康观测不是配置编辑，保持修订号不变，后续委托命令仍针对同一配置。
        revision=connector.revision,
        created_by=connector.created_by,
        updated_by=connector.updated_by,
        created_at=connector.created_at,
        updated_at=now,
    )
    connectors.save(updated_connector, expected_revision=connector.revision)
    command_status = (
        PendingCommandStatus.SUCCEEDED
        if reachability is ConnectorReachability.REACHABLE
        else PendingCommandStatus.FAILED
    )
    completed = commands.complete(
        PendingCommandCompletion(
            command_id=command.id,
            host_id=host_id,
            claim_token=claim_token,
            status=command_status,
            result=result.reachability,
            result_detail=result.detail,
            failure_code=None,
            completed_at=now,
            result_credentials_configured=result.credentials_configured,
        )
    )
    _logger.info(
        "device.pending_command.completed",
        command_id=str(command.id),
        host_id=str(host_id),
        outcome=reachability.value,
    )
    return completed


def _reject(
    command: PendingCommand,
    *,
    code: DeviceRefusalCode | str,
    detail: str | None = None,
    credentials_configured: bool | None = None,
    now: datetime,
    host_id: UUID,
    commands: PendingCommandRepository,
) -> PendingCommand:
    """把无法安全应用的机器回报结案为拒绝，而不是伪造连接失败。"""
    failure_code = code.value if isinstance(code, DeviceRefusalCode) else code
    rejected = commands.complete(
        PendingCommandCompletion(
            command_id=command.id,
            host_id=host_id,
            claim_token=command.claim_token or "",
            status=PendingCommandStatus.REJECTED,
            result=None,
            result_detail=detail,
            failure_code=failure_code,
            completed_at=now,
            result_credentials_configured=credentials_configured,
        )
    )
    _logger.info(
        "device.pending_command.rejected",
        command_id=str(command.id),
        host_id=str(host_id),
        error_code=failure_code,
    )
    return rejected


def _same_result(command: PendingCommand, result: ConnectorTestResult) -> bool:
    """比较已结案命令的可持久化结果, 避免把不同回报误当成幂等重试。"""
    return (
        command.result == result.reachability
        and command.result_detail == result.detail
        and command.failure_code == result.failure_code
        and command.result_credentials_configured == result.credentials_configured
    )


def _existing_command(command_id: UUID, commands: PendingCommandRepository) -> PendingCommand:
    command = commands.by_id(command_id)
    if command is None:
        refuse(_REFUSAL_EVENT, DeviceRefusalCode.COMMAND_NOT_FOUND, command_id=str(command_id))
    return command


def _existing_command_for_update(
    command_id: UUID, commands: PendingCommandRepository
) -> PendingCommand:
    command = commands.by_id_for_update(command_id)
    if command is None:
        refuse(_REFUSAL_EVENT, DeviceRefusalCode.COMMAND_NOT_FOUND, command_id=str(command_id))
    return command


def _existing_connector(connector_id: UUID, connectors: ConnectorRepository) -> Connector:
    connector = connectors.by_id(connector_id)
    if connector is None:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.CONNECTOR_NOT_FOUND,
            connector_id=str(connector_id),
        )
    return connector
