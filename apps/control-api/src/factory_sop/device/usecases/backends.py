"""推理后端的配置用例：创建、编辑、停用、恢复、删除和读取。

拓扑约束在这里作为行为实现，而不是调用方的职责：后端只能创建在已存在且处于活动状态的推理机上，
也只能移动到这样的推理机。外键和迁移触发器为绕过本层的调用方提供数据库兜底，包括相机引用使
推理机或模板迁移变得不安全的情况。每个用例先授权调用方，使用操作对应的
`device.inference_backend.*` 权限。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.device.errors import DeviceRefusalCode
from factory_sop.device.model import (
    ConnectionState,
    DeviceStatus,
    InferenceBackend,
    InferenceHost,
)
from factory_sop.device.repository import InferenceBackendRepository, InferenceHostRepository
from factory_sop.device.usecases._transitions import refuse, set_status
from factory_sop.identifiers import new_id
from factory_sop.observability import get_logger

_logger = get_logger("device")

_REFUSAL_EVENT = "device.inference_backend.refused"


def create_backend(
    *,
    host_id: UUID,
    base_url: str,
    caller: Caller,
    now: datetime,
    hosts: InferenceHostRepository,
    backends: InferenceBackendRepository,
) -> InferenceBackend:
    """Register one process endpoint on its host. The host must exist and be active."""
    authorize(caller, Permission.INFERENCE_BACKEND_EDIT)
    _active_host(host_id, hosts)
    backend = InferenceBackend(
        id=new_id(),
        host_id=host_id,
        base_url=base_url,
        # The template a backend carries is bound by the `template` module's use cases (C5);
        # until then no writer exists and every row reads `None`.
        template_version_id=None,
        status=DeviceStatus.ACTIVE,
        # Q31: the record is 未验证 until a real test has run, and creation never probes —
        # saving a configuration offline is exactly what the contract protects.
        connection_state=ConnectionState.UNVERIFIED,
        connection_checked_at=None,
        connection_detail=None,
        self_reported_model_ids=(),
        self_reported_at=None,
        revision=1,
        created_by=caller.user.id,
        updated_by=caller.user.id,
        created_at=now,
        updated_at=now,
    )
    backends.add(backend)
    _logger.info(
        "device.inference_backend.created",
        backend_id=str(backend.id),
        host_id=str(host_id),
        actor_id=str(caller.user.id),
    )
    return backend


def edit_backend(
    *,
    backend_id: UUID,
    host_id: UUID,
    base_url: str,
    expected_revision: int,
    caller: Caller,
    now: datetime,
    hosts: InferenceHostRepository,
    backends: InferenceBackendRepository,
) -> InferenceBackend:
    """Replace the backend's placement and endpoint, refusing a lost revision race.

    The connection facts describe the placement they were observed at (§5.13 attributes the
    model identity to the host), so moving the backend or pointing it at another endpoint
    retires them: the record goes back to 未验证, and only a new test — not an edit — may
    repopulate it.
    """
    authorize(caller, Permission.INFERENCE_BACKEND_EDIT)
    backend = _existing_backend(backend_id, backends)
    if host_id == backend.host_id:
        # 停用 removes a host from new bindings and operation, but it does not freeze its
        # historical configuration. An administrator may repair an endpoint in place before
        # restoring the host; moving a backend remains a new binding and still needs an active
        # target host.
        _existing_host(host_id, hosts)
    else:
        _active_host(host_id, hosts)
    placement_changed = (host_id, base_url) != (backend.host_id, backend.base_url)
    if placement_changed:
        edited = replace(
            backend,
            host_id=host_id,
            base_url=base_url,
            connection_state=ConnectionState.UNVERIFIED,
            connection_checked_at=None,
            connection_detail=None,
            self_reported_model_ids=(),
            self_reported_at=None,
            revision=expected_revision + 1,
            updated_by=caller.user.id,
            updated_at=now,
        )
    else:
        edited = replace(
            backend,
            host_id=host_id,
            base_url=base_url,
            revision=expected_revision + 1,
            updated_by=caller.user.id,
            updated_at=now,
        )
    backends.save(edited, expected_revision=expected_revision)
    _logger.info(
        "device.inference_backend.updated",
        backend_id=str(backend.id),
        host_id=str(host_id),
        placement_changed=placement_changed,
        actor_id=str(caller.user.id),
    )
    return edited


def deactivate_backend(
    *,
    backend_id: UUID,
    expected_revision: int,
    caller: Caller,
    now: datetime,
    backends: InferenceBackendRepository,
) -> InferenceBackend:
    """Take the endpoint out of new bindings at the revision the caller read."""
    authorize(caller, Permission.INFERENCE_BACKEND_EDIT)
    backend = _existing_backend(backend_id, backends)
    _require_revision(backend, expected_revision)
    if backend.status is DeviceStatus.DEACTIVATED:
        return backend
    return set_status(
        backend,
        status=DeviceStatus.DEACTIVATED,
        event="device.inference_backend.deactivated",
        actor_id=caller.user.id,
        now=now,
        context={"backend_id": str(backend.id)},
        save=backends.save,
    )


def restore_backend(
    *,
    backend_id: UUID,
    expected_revision: int,
    caller: Caller,
    now: datetime,
    backends: InferenceBackendRepository,
) -> InferenceBackend:
    """Bring a deactivated backend back at the revision the caller read."""
    authorize(caller, Permission.INFERENCE_BACKEND_EDIT)
    backend = _existing_backend(backend_id, backends)
    _require_revision(backend, expected_revision)
    if backend.status is DeviceStatus.ACTIVE:
        return backend
    return set_status(
        backend,
        status=DeviceStatus.ACTIVE,
        event="device.inference_backend.restored",
        actor_id=caller.user.id,
        now=now,
        context={"backend_id": str(backend.id)},
        save=backends.save,
    )


def delete_backend(
    *,
    backend_id: UUID,
    expected_revision: int,
    caller: Caller,
    backends: InferenceBackendRepository,
) -> None:
    """Remove the process endpoint at the revision the caller read."""
    authorize(caller, Permission.INFERENCE_BACKEND_DELETE)
    backend = _existing_backend(backend_id, backends)
    _require_revision(backend, expected_revision)
    if not backends.remove(backend_id, expected_revision=expected_revision):
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.INFERENCE_BACKEND_NOT_FOUND,
            backend_id=str(backend_id),
            actor_id=str(caller.user.id),
        )
    _logger.info(
        "device.inference_backend.deleted",
        backend_id=str(backend_id),
        actor_id=str(caller.user.id),
    )


def backend_by_identifier(
    *, backend_id: UUID, caller: Caller, backends: InferenceBackendRepository
) -> InferenceBackend:
    """Read one backend. Refuses `INFERENCE_BACKEND_NOT_FOUND` when it is gone."""
    authorize(caller, Permission.INFERENCE_BACKEND_VIEW)
    return _existing_backend(backend_id, backends)


def list_backends(
    *,
    caller: Caller,
    backends: InferenceBackendRepository,
    page: int,
    page_size: int,
    host_id: UUID | None,
) -> tuple[list[InferenceBackend], int]:
    """One page of backends — every host's, or one host's — newest first, with the total.

    `host_id=None` is the unfiltered listing; the filter's absence is what "all" means here.
    """
    authorize(caller, Permission.INFERENCE_BACKEND_VIEW)
    return backends.page_of(page=page, page_size=page_size, host_id=host_id)


def _existing_backend(backend_id: UUID, backends: InferenceBackendRepository) -> InferenceBackend:
    backend = backends.by_id(backend_id)
    if backend is None:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.INFERENCE_BACKEND_NOT_FOUND,
            backend_id=str(backend_id),
        )
    return backend


def _require_revision(backend: InferenceBackend, expected_revision: int) -> None:
    if backend.revision != expected_revision:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.STALE_REVISION,
            backend_id=str(backend.id),
            expected_revision=str(expected_revision),
            actual_revision=str(backend.revision),
        )


def _existing_host(host_id: UUID, hosts: InferenceHostRepository) -> InferenceHost:
    """Resolve a host without imposing the active-only rule used by new bindings."""
    host = hosts.by_id(host_id)
    if host is None:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND,
            host_id=str(host_id),
        )
    return host


def _active_host(host_id: UUID, hosts: InferenceHostRepository) -> InferenceHost:
    """The host a new backend binding hangs off must be active (CONTEXT.md: 停用)."""
    host = _existing_host(host_id, hosts)
    if host.status is DeviceStatus.DEACTIVATED:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED,
            host_id=str(host_id),
        )
    return host
