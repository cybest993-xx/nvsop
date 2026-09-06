"""The inference backend's configuration use cases: create, edit, 停用, restore, delete, read.

The topology constraint lives here as behavior, not as a caller's duty: a backend is created
and moved only onto a host that exists and is active, the database's foreign key holds the
existence half for whoever bypasses this layer, and the migration's trigger holds the
active-half for them too. Each use case authorizes its caller first, with the
`device.inference_backend.*` permission the operation is.
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
    *, backend_id: UUID, caller: Caller, now: datetime, backends: InferenceBackendRepository
) -> InferenceBackend:
    """Take the endpoint out of new bindings. Reversible; a replay changes nothing."""
    authorize(caller, Permission.INFERENCE_BACKEND_EDIT)
    backend = _existing_backend(backend_id, backends)
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
    *, backend_id: UUID, caller: Caller, now: datetime, backends: InferenceBackendRepository
) -> InferenceBackend:
    """Bring a deactivated backend back. Reversible's other half; a replay changes nothing."""
    authorize(caller, Permission.INFERENCE_BACKEND_EDIT)
    backend = _existing_backend(backend_id, backends)
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
    *, backend_id: UUID, caller: Caller, backends: InferenceBackendRepository
) -> None:
    """Remove the process endpoint outright. Cameras will add their own reference rules."""
    authorize(caller, Permission.INFERENCE_BACKEND_DELETE)
    _existing_backend(backend_id, backends)
    if not backends.remove(backend_id):
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
