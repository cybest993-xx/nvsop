"""中心委托命令的业务接缝：入队、领取、回报和重试。"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.device.errors import DeviceRefusalCode
from factory_sop.device.model import (
    ClaimedReclipCommand,
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
    command_type: PendingCommandType,
    now: datetime,
    lease_duration: timedelta,
    hosts: InferenceHostRepository,
    commands: PendingCommandRepository,
) -> PendingCommand | None:
    """认证后原子领取该推理机指定类型的下一条命令。"""
    authenticate_command_host(host=host, now=now, hosts=hosts)
    if lease_duration <= timedelta(0):
        raise ValueError("command lease duration must be positive")
    token = str(new_id())
    claimed = commands.claim_next(
        host_id=host.host_id,
        command_type=command_type,
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


def complete_reclip_command(
    *,
    command_id: UUID,
    host: InferenceHostIdentity,
    claim_token: str,
    now: datetime,
    hosts: InferenceHostRepository,
    commands: PendingCommandRepository,
    failure_code: str | None = None,
    detail: str | None = None,
) -> PendingCommand:
    """认证领取主机后把一条再切片命令结案为 ``SUCCEEDED`` 或终局 ``FAILED``。

    失败结案带来源机给出的机器可读原因；同一令牌的相同结案重放幂等，失败不得覆盖已成功结案，
    命令类型不匹配或令牌/租约非法时明确拒绝。认证始终执行, 不提供跳过开关。
    """
    authenticate_command_host(host=host, now=now, hosts=hosts)
    return _settle_reclip_command(
        command_id=command_id,
        host_id=host.host_id,
        claim_token=claim_token,
        now=now,
        commands=commands,
        failure_code=failure_code,
        detail=detail,
    )


def fail_claimed_reclip_command(
    *,
    claimed: ClaimedReclipCommand,
    host_id: UUID,
    failure_code: str,
    detail: str | None,
    now: datetime,
    commands: PendingCommandRepository,
) -> PendingCommand:
    """把同一请求内刚领取的再切片命令终局失败。

    领取已由 ``claim_next_command`` 完成主机认证；本入口只接受那条领取回执, 仍走同一结案校验
    （主机/令牌/租约/类型/终态), 不重复认证也不放开跳过开关。仅供领取端点发现请求/来源消失。
    """
    return _settle_reclip_command(
        command_id=claimed.command_id,
        host_id=host_id,
        claim_token=claimed.claim_token,
        now=now,
        commands=commands,
        failure_code=failure_code,
        detail=detail,
    )


def _settle_reclip_command(
    *,
    command_id: UUID,
    host_id: UUID,
    claim_token: str,
    now: datetime,
    commands: PendingCommandRepository,
    failure_code: str | None,
    detail: str | None,
) -> PendingCommand:
    """认证后的再切片命令结案唯一实现; 主机/令牌/租约/类型/终态校验在此一处维护。"""
    status = (
        PendingCommandStatus.FAILED if failure_code is not None else PendingCommandStatus.SUCCEEDED
    )
    command = _existing_command_for_update(command_id, commands)
    if command.host_id != host_id:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.COMMAND_HOST_MISMATCH,
            command_id=str(command_id),
            host_id=str(host_id),
        )
    if command.command_type is not PendingCommandType.RECLIP_EVIDENCE:
        refuse(_REFUSAL_EVENT, DeviceRefusalCode.COMMAND_NOT_FOUND, command_id=str(command_id))
    if command.status in {
        PendingCommandStatus.SUCCEEDED,
        PendingCommandStatus.FAILED,
        PendingCommandStatus.REJECTED,
    }:
        if (
            command.claim_token == claim_token
            and command.status is status
            and command.failure_code == failure_code
            and command.result_detail == detail
        ):
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
    completed = commands.complete(
        PendingCommandCompletion(
            command_id=command.id,
            host_id=host_id,
            claim_token=claim_token,
            status=status,
            result=None,
            result_detail=detail,
            failure_code=failure_code,
            completed_at=now,
        )
    )
    _logger.info(
        "device.pending_command.completed",
        command_id=str(command.id),
        host_id=str(host_id),
        outcome="reclip_succeeded" if status is PendingCommandStatus.SUCCEEDED else "reclip_failed",
        error_code=failure_code,
    )
    return completed


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


class RepositoryDeviceExecutionHandoverCommandGateway:
    """execution 使用的正常交权命令 owner；不把命令队列复制到 execution。"""

    def __init__(
        self,
        *,
        hosts: InferenceHostRepository,
        commands: PendingCommandRepository,
    ) -> None:
        self._hosts = hosts
        self._commands = commands

    def enqueue(
        self,
        *,
        command_id: UUID,
        handover_id: UUID,
        host_id: UUID,
        requested_by: UUID,
        now: datetime,
    ) -> None:
        command = PendingCommand(
            id=command_id,
            host_id=host_id,
            command_type=PendingCommandType.STOP_EXECUTION_HANDOVER,
            target_id=handover_id,
            target_revision=1,
            idempotency_key=f"execution-handover:{handover_id}",
            status=PendingCommandStatus.PENDING,
            attempt=0,
            claim_token=None,
            claimed_at=None,
            lease_expires_at=None,
            result=None,
            result_detail=None,
            failure_code=None,
            completed_at=None,
            created_by=requested_by,
            created_at=now,
            updated_at=now,
        )
        stored = self._commands.add_or_get(command)
        if (
            stored.command_type is not PendingCommandType.STOP_EXECUTION_HANDOVER
            or stored.target_id != handover_id
            or stored.host_id != host_id
        ):
            refuse(
                _REFUSAL_EVENT,
                DeviceRefusalCode.COMMAND_IDEMPOTENCY_CONFLICT,
                idempotency_key=command.idempotency_key,
                actor_id=str(requested_by),
            )

    def claim(
        self,
        *,
        host: InferenceHostIdentity,
        now: datetime,
        lease_duration: timedelta,
    ) -> PendingCommand | None:
        return claim_next_command(
            host=host,
            command_type=PendingCommandType.STOP_EXECUTION_HANDOVER,
            now=now,
            lease_duration=lease_duration,
            hosts=self._hosts,
            commands=self._commands,
        )

    def lock_claim(
        self,
        *,
        command_id: UUID,
        now: datetime,
        host: InferenceHostIdentity | None = None,
    ) -> bool:
        if host is not None:
            authenticate_command_host(host=host, now=now, hosts=self._hosts)
        command = _existing_command_for_update(command_id, self._commands)
        if command.command_type is not PendingCommandType.STOP_EXECUTION_HANDOVER:
            refuse(_REFUSAL_EVENT, DeviceRefusalCode.COMMAND_NOT_FOUND, command_id=str(command_id))
        return command.attempt > 0

    def complete(
        self,
        *,
        command_id: UUID,
        host: InferenceHostIdentity,
        claim_token: str,
        now: datetime,
        outcome: str,
        failure_code: str | None = None,
        detail: str | None = None,
        authenticated: bool = False,
    ) -> PendingCommand:
        if not authenticated:
            authenticate_command_host(host=host, now=now, hosts=self._hosts)
        status_by_outcome = {
            "confirmed": PendingCommandStatus.SUCCEEDED,
            "failed": PendingCommandStatus.FAILED,
            "rejected": PendingCommandStatus.REJECTED,
        }
        status = status_by_outcome.get(outcome)
        if status is None:
            raise ValueError("execution handover outcome is unsupported")
        command = _existing_command_for_update(command_id, self._commands)
        if command.command_type is not PendingCommandType.STOP_EXECUTION_HANDOVER:
            refuse(_REFUSAL_EVENT, DeviceRefusalCode.COMMAND_NOT_FOUND, command_id=str(command_id))
        if command.host_id != host.host_id:
            refuse(
                _REFUSAL_EVENT,
                DeviceRefusalCode.COMMAND_HOST_MISMATCH,
                command_id=str(command_id),
                host_id=str(host.host_id),
            )
        if command.status in {
            PendingCommandStatus.SUCCEEDED,
            PendingCommandStatus.FAILED,
            PendingCommandStatus.REJECTED,
        }:
            if (
                command.claim_token == claim_token
                and command.status is status
                and command.failure_code == failure_code
                and command.result_detail == detail
            ):
                return command
            refuse(
                _REFUSAL_EVENT,
                DeviceRefusalCode.COMMAND_ALREADY_COMPLETED,
                command_id=str(command_id),
                host_id=str(host.host_id),
            )
        if command.status is not PendingCommandStatus.CLAIMED:
            refuse(
                _REFUSAL_EVENT,
                DeviceRefusalCode.COMMAND_CLAIM_REQUIRED,
                command_id=str(command_id),
                host_id=str(host.host_id),
            )
        if command.claim_token != claim_token:
            refuse(
                _REFUSAL_EVENT,
                DeviceRefusalCode.COMMAND_CLAIM_TOKEN_INVALID,
                command_id=str(command_id),
                host_id=str(host.host_id),
            )
        if command.lease_expires_at is None or now >= command.lease_expires_at:
            refuse(
                _REFUSAL_EVENT,
                DeviceRefusalCode.COMMAND_CLAIM_EXPIRED,
                command_id=str(command_id),
                host_id=str(host.host_id),
            )
        return self._commands.complete(
            PendingCommandCompletion(
                command_id=command.id,
                host_id=host.host_id,
                claim_token=claim_token,
                status=status,
                result=None,
                result_detail=detail,
                failure_code=failure_code,
                completed_at=now,
            )
        )


class RepositoryDeviceEvidenceGateway:
    """`evidence` 再切片请求所需的 device 最小事实：来源主机滚动窗口与委托命令登记。

    命令在调用方的请求事务内写入同一队列；领取只走专用再切片签名端点（按命令类型收窄，默认
    `/device-commands/next` 不取），故 `target_revision` 无对应配置，固定 1 保留队列非空语义。
    """

    def __init__(
        self,
        *,
        hosts: InferenceHostRepository,
        commands: PendingCommandRepository,
    ) -> None:
        self._hosts = hosts
        self._commands = commands

    def recording_window_seconds(self, *, host_id: UUID) -> int | None:
        host = self._hosts.by_id(host_id)
        return None if host is None else host.recording_window_seconds

    def enqueue_reclip_command(
        self,
        *,
        host_id: UUID,
        request_id: UUID,
        idempotency_key: str,
        actor_id: UUID,
        now: datetime,
    ) -> UUID:
        command = PendingCommand(
            id=new_id(),
            host_id=host_id,
            command_type=PendingCommandType.RECLIP_EVIDENCE,
            target_id=request_id,
            target_revision=1,
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
            created_by=actor_id,
            created_at=now,
            updated_at=now,
        )
        stored = self._commands.add_or_get(command)
        if (
            stored.command_type is not PendingCommandType.RECLIP_EVIDENCE
            or stored.target_id != request_id
        ):
            refuse(
                _REFUSAL_EVENT,
                DeviceRefusalCode.COMMAND_IDEMPOTENCY_CONFLICT,
                idempotency_key=idempotency_key,
                actor_id=str(actor_id),
            )
        return stored.id

    def claim_reclip_command(
        self, *, host: InferenceHostIdentity, now: datetime, lease_duration: timedelta
    ) -> ClaimedReclipCommand | None:
        claimed = claim_next_command(
            host=host,
            command_type=PendingCommandType.RECLIP_EVIDENCE,
            now=now,
            lease_duration=lease_duration,
            hosts=self._hosts,
            commands=self._commands,
        )
        if claimed is None or claimed.claim_token is None or claimed.lease_expires_at is None:
            return None
        return ClaimedReclipCommand(
            command_id=claimed.id,
            request_id=claimed.target_id,
            claim_token=claimed.claim_token,
            lease_expires_at=claimed.lease_expires_at,
        )

    def complete_reclip_command(
        self,
        *,
        command_id: UUID,
        host: InferenceHostIdentity,
        claim_token: str,
        now: datetime,
        failure_code: str | None = None,
        detail: str | None = None,
    ) -> None:
        complete_reclip_command(
            command_id=command_id,
            host=host,
            claim_token=claim_token,
            now=now,
            hosts=self._hosts,
            commands=self._commands,
            failure_code=failure_code,
            detail=detail,
        )

    def fail_claimed_reclip_command(
        self,
        *,
        claimed: ClaimedReclipCommand,
        host_id: UUID,
        failure_code: str,
        detail: str | None,
        now: datetime,
    ) -> None:
        fail_claimed_reclip_command(
            claimed=claimed,
            host_id=host_id,
            failure_code=failure_code,
            detail=detail,
            now=now,
            commands=self._commands,
        )
