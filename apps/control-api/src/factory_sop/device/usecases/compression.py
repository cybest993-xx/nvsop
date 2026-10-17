"""推理机录像压缩的委托控制, 中心只保存请求与来源机观测。"""

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.model import (
    DeviceStatus,
    InferenceHostIdentity,
    PendingCommand,
    PendingCommandCompletion,
    PendingCommandStatus,
    PendingCommandType,
)
from factory_sop.device.repository import InferenceHostRepository, PendingCommandRepository
from factory_sop.device.usecases._transitions import refuse
from factory_sop.device.usecases.commands import (
    _existing_command_for_update,
    authenticate_command_host,
)
from factory_sop.identifiers import new_id
from factory_sop.observability import get_logger
from nvsop_contracts.compression import (
    CompressionAction,
    CompressionResult,
    compression_action_confirmed,
)

_EVENT = "device.compression.refused"
_logger = get_logger("device")


def enqueue_compression(
    *,
    host_id: UUID,
    action: CompressionAction,
    idempotency_key: str,
    caller: Caller,
    now: datetime,
    hosts: InferenceHostRepository,
    commands: PendingCommandRepository,
) -> PendingCommand:
    # 入队即控制: 所有动作(含 inspect)都要求编辑权限, 与 OpenAPI 声明的权限一致。
    authorize(caller, Permission.INFERENCE_HOST_EDIT)
    context = {"host_id": str(host_id), "actor_id": str(caller.user.id), "action": action}
    existing = commands.by_idempotency_key(idempotency_key)
    if existing is None:
        host = hosts.by_id(host_id)
        if host is None:
            refuse(_EVENT, DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND, **context)
        if host.status is DeviceStatus.DEACTIVATED:
            refuse(_EVENT, DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED, **context)
        command_id = new_id()
        existing = commands.add_or_get(
            PendingCommand(
                id=command_id,
                host_id=host_id,
                command_type=PendingCommandType.CONTROL_RECORDING_COMPRESSION,
                target_id=host_id,
                target_revision=host.revision,
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
                compression_action=action,
            )
        )
        if existing.id == command_id:
            _logger.info("device.compression.created", command_id=str(command_id), **context)
    if (
        existing.host_id != host_id
        or existing.compression_action != action
        or existing.command_type is not PendingCommandType.CONTROL_RECORDING_COMPRESSION
    ):
        refuse(_EVENT, DeviceRefusalCode.COMMAND_IDEMPOTENCY_CONFLICT, **context)
    return existing


def read_compression_command(
    *,
    host_id: UUID,
    command_id: UUID,
    caller: Caller,
    commands: PendingCommandRepository,
) -> PendingCommand:
    authorize(caller, Permission.INFERENCE_HOST_VIEW)
    command = commands.by_id(command_id)
    if (
        command is None
        or command.host_id != host_id
        or command.command_type is not PendingCommandType.CONTROL_RECORDING_COMPRESSION
    ):
        refuse(
            _EVENT,
            DeviceRefusalCode.COMMAND_NOT_FOUND,
            host_id=str(host_id),
            command_id=str(command_id),
            actor_id=str(caller.user.id),
        )
    # 回退到压缩控制之前的 schema 后重新升级: 历史行没有可解释的动作, 不可解释即不可操作。
    if command.compression_action is None:
        refuse(
            _EVENT,
            DeviceRefusalCode.COMMAND_NOT_FOUND,
            host_id=str(host_id),
            command_id=str(command_id),
            actor_id=str(caller.user.id),
        )
    return command


def complete_compression(
    *,
    command_id: UUID,
    host: InferenceHostIdentity,
    claim_token: str,
    result: CompressionResult,
    now: datetime,
    hosts: InferenceHostRepository,
    commands: PendingCommandRepository,
) -> PendingCommand:
    authenticate_command_host(host=host, now=now, hosts=hosts)
    context = {"host_id": str(host.host_id), "command_id": str(command_id)}
    try:
        command = _existing_command_for_update(command_id, commands)
    except DeviceRefusedError as error:
        _logger.info(_EVENT, error_code=error.code.value, **context)
        raise
    if command.host_id != host.host_id:
        refuse(_EVENT, DeviceRefusalCode.COMMAND_HOST_MISMATCH, **context)
    if command.command_type is not PendingCommandType.CONTROL_RECORDING_COMPRESSION:
        refuse(_EVENT, DeviceRefusalCode.COMMAND_NOT_FOUND, **context)
    if command.compression_action is None:
        refuse(_EVENT, DeviceRefusalCode.COMMAND_NOT_FOUND, **context)
    if result.failure_code is None and not compression_action_confirmed(
        cast(CompressionAction, command.compression_action), result.state
    ):
        refuse(_EVENT, DeviceRefusalCode.COMMAND_OBSERVATION_UNCONFIRMED, **context)
    try:
        completed = commands.complete(
            PendingCommandCompletion(
                command_id=command.id,
                host_id=host.host_id,
                claim_token=claim_token,
                status=PendingCommandStatus.FAILED
                if result.failure_code
                else PendingCommandStatus.SUCCEEDED,
                result=None,
                result_detail=result.detail,
                failure_code=result.failure_code,
                completed_at=now,
                compression_result=result.to_wire(),
            )
        )
    except DeviceRefusedError as error:
        _logger.info(_EVENT, error_code=error.code.value, **context)
        raise
    _logger.info("device.compression.completed", status=completed.status.value, **context)
    return completed
