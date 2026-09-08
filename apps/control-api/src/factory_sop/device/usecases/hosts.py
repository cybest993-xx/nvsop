"""推理机配置用例：创建、编辑、停用、恢复、删除和读取。

所有时刻和调用方都由参数传入；用例不读取时钟，也不知道会话，因此任何调用方都与 HTTP 层遵循同一规则。
每个用例开头调用 `authorize`（§5.15），只有持有 `device.inference_host.*` 权限的调用方可以继续。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import NoReturn
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.device.errors import DeviceRefusalCode
from factory_sop.device.model import DeviceStatus, InferenceHost
from factory_sop.device.repository import (
    ConnectorRepository,
    InferenceBackendRepository,
    InferenceHostRepository,
)
from factory_sop.device.usecases._transitions import refuse, set_status
from factory_sop.identifiers import new_id
from factory_sop.observability import get_logger

_logger = get_logger("device")

_REFUSAL_EVENT = "device.inference_host.refused"


def create_host(
    *,
    name: str,
    address: str,
    mediamtx_address: str | None,
    recording_window_seconds: int,
    disk_watermark_percent: int,
    caller: Caller,
    now: datetime,
    hosts: InferenceHostRepository,
) -> InferenceHost:
    """登记物理推理机；名称冲突或缺少 `INFERENCE_HOST_EDIT` 权限时拒绝。"""
    authorize(caller, Permission.INFERENCE_HOST_EDIT)
    host = InferenceHost(
        id=new_id(),
        name=name,
        address=address,
        mediamtx_address=mediamtx_address,
        recording_window_seconds=recording_window_seconds,
        disk_watermark_percent=disk_watermark_percent,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=caller.user.id,
        updated_by=caller.user.id,
        created_at=now,
        updated_at=now,
    )
    hosts.add(host)
    _logger.info(
        "device.inference_host.created",
        host_id=str(host.id),
        name=host.name,
        actor_id=str(caller.user.id),
    )
    return host


def edit_host(
    *,
    host_id: UUID,
    name: str,
    address: str,
    mediamtx_address: str | None,
    recording_window_seconds: int,
    disk_watermark_percent: int,
    expected_revision: int,
    caller: Caller,
    now: datetime,
    hosts: InferenceHostRepository,
) -> InferenceHost:
    """替换推理机完整配置，并拒绝版本竞争丢失。

    请求体是完整配置而不是部分补丁；停用状态不在此编辑，恢复由独立用例处理。
    """
    authorize(caller, Permission.INFERENCE_HOST_EDIT)
    host = _existing_host(host_id, hosts)
    edited = replace(
        host,
        name=name,
        address=address,
        mediamtx_address=mediamtx_address,
        recording_window_seconds=recording_window_seconds,
        disk_watermark_percent=disk_watermark_percent,
        revision=expected_revision + 1,
        updated_by=caller.user.id,
        updated_at=now,
    )
    hosts.save(edited, expected_revision=expected_revision)
    _logger.info(
        "device.inference_host.updated", host_id=str(host.id), actor_id=str(caller.user.id)
    )
    return edited


def register_host_identity_key(
    *,
    host_id: UUID,
    public_key: str,
    expected_revision: int,
    caller: Caller,
    now: datetime,
    hosts: InferenceHostRepository,
) -> InferenceHost:
    """登记推理机公钥；私钥只由推理机本地持有。"""
    authorize(caller, Permission.INFERENCE_HOST_EDIT)
    host = _existing_host(host_id, hosts)
    _require_revision(host, expected_revision)
    registered = replace(
        host,
        identity_public_key=public_key,
        revision=expected_revision + 1,
        updated_by=caller.user.id,
        updated_at=now,
    )
    hosts.save(registered, expected_revision=expected_revision)
    _logger.info(
        "device.inference_host.identity_key_registered",
        host_id=str(host_id),
        actor_id=str(caller.user.id),
    )
    return registered


def retire_host_credential(*, host_id: UUID, caller: Caller) -> NoReturn:
    """拒绝旧 bearer 凭据接口；中心不再签发或保存共享秘密。"""
    authorize(caller, Permission.INFERENCE_HOST_EDIT)
    refuse(
        _REFUSAL_EVENT,
        DeviceRefusalCode.INFERENCE_HOST_CREDENTIALS_REMOVED,
        host_id=str(host_id),
        actor_id=str(caller.user.id),
    )


def deactivate_host(
    *,
    host_id: UUID,
    expected_revision: int,
    caller: Caller,
    now: datetime,
    hosts: InferenceHostRepository,
) -> InferenceHost:
    """按调用方读取的版本停用推理机，使其退出新绑定和运行。"""
    authorize(caller, Permission.INFERENCE_HOST_EDIT)
    host = _existing_host(host_id, hosts)
    _require_revision(host, expected_revision)
    if host.status is DeviceStatus.DEACTIVATED:
        return host
    return set_status(
        host,
        status=DeviceStatus.DEACTIVATED,
        event="device.inference_host.deactivated",
        actor_id=caller.user.id,
        now=now,
        context={"host_id": str(host.id)},
        save=hosts.save,
    )


def restore_host(
    *,
    host_id: UUID,
    expected_revision: int,
    caller: Caller,
    now: datetime,
    hosts: InferenceHostRepository,
) -> InferenceHost:
    """按调用方读取的版本恢复已停用推理机。"""
    authorize(caller, Permission.INFERENCE_HOST_EDIT)
    host = _existing_host(host_id, hosts)
    _require_revision(host, expected_revision)
    if host.status is DeviceStatus.ACTIVE:
        return host
    return set_status(
        host,
        status=DeviceStatus.ACTIVE,
        event="device.inference_host.restored",
        actor_id=caller.user.id,
        now=now,
        context={"host_id": str(host.id)},
        save=hosts.save,
    )


def delete_host(
    *,
    host_id: UUID,
    expected_revision: int,
    caller: Caller,
    hosts: InferenceHostRepository,
    backends: InferenceBackendRepository,
    connectors: ConnectorRepository,
) -> None:
    """仅在推理后端和连接器关联都移除后删除推理机。"""
    authorize(caller, Permission.INFERENCE_HOST_DELETE)
    host = _existing_host(host_id, hosts)
    _require_revision(host, expected_revision)
    if backends.any_for_host(host_id):
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.INFERENCE_HOST_HAS_BACKENDS,
            host_id=str(host_id),
            actor_id=str(caller.user.id),
        )
    if connectors.any_for_host(host_id):
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.INFERENCE_HOST_HAS_CONNECTORS,
            host_id=str(host_id),
            actor_id=str(caller.user.id),
        )
    if not hosts.remove(host_id, expected_revision=expected_revision):
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND,
            host_id=str(host_id),
            actor_id=str(caller.user.id),
        )
    _logger.info(
        "device.inference_host.deleted", host_id=str(host_id), actor_id=str(caller.user.id)
    )


def host_by_identifier(
    *, host_id: UUID, caller: Caller, hosts: InferenceHostRepository
) -> InferenceHost:
    """读取一台推理机；记录不存在时拒绝并返回 `INFERENCE_HOST_NOT_FOUND`。"""
    authorize(caller, Permission.INFERENCE_HOST_VIEW)
    return _existing_host(host_id, hosts)


def list_hosts(
    *, caller: Caller, hosts: InferenceHostRepository, page: int, page_size: int
) -> tuple[list[InferenceHost], int]:
    """按最新优先返回一页推理机及未分页总数（§5.15）。"""
    authorize(caller, Permission.INFERENCE_HOST_VIEW)
    return hosts.page_of(page=page, page_size=page_size)


def _require_revision(host: InferenceHost, expected_revision: int) -> None:
    if host.revision != expected_revision:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.STALE_REVISION,
            host_id=str(host.id),
            expected_revision=str(expected_revision),
            actual_revision=str(host.revision),
        )


def _existing_host(host_id: UUID, hosts: InferenceHostRepository) -> InferenceHost:
    host = hosts.by_id(host_id)
    if host is None:
        refuse(_REFUSAL_EVENT, DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND, host_id=str(host_id))
    return host
