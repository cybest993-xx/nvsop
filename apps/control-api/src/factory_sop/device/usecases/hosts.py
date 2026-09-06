"""The inference host's configuration use cases: create, edit, 停用, restore, delete, read.

Every instant and every caller arrives as an argument: the use cases read no clock and know
no session, so any caller drives them exactly as the HTTP layer does. Authorization is the
`authorize` call at the top of each one (§5.15) — a `Caller` with `device.inference_host.*`
may proceed, anyone else is refused, whichever kind of caller the request came from.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.device.errors import DeviceRefusalCode
from factory_sop.device.model import DeviceStatus, InferenceHost
from factory_sop.device.repository import InferenceBackendRepository, InferenceHostRepository
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
    """Register a physical inference machine.

    Refuses a taken name with `INFERENCE_HOST_NAME_TAKEN`, and anyone without
    `INFERENCE_HOST_EDIT` before anything else.
    """
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
    """Replace the host's whole editable configuration, refusing a lost revision race.

    The body is the complete configuration rather than a partial patch: the caller holds the
    record already — it read the revision it sends as `expected_revision`. 停用 status is not
    editable here: restoring is its own use case.
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


def deactivate_host(
    *, host_id: UUID, caller: Caller, now: datetime, hosts: InferenceHostRepository
) -> InferenceHost:
    """Take the host out of new bindings and operation. Reversible; a replay changes nothing."""
    authorize(caller, Permission.INFERENCE_HOST_EDIT)
    host = _existing_host(host_id, hosts)
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
    *, host_id: UUID, caller: Caller, now: datetime, hosts: InferenceHostRepository
) -> InferenceHost:
    """Bring a deactivated host back. Reversible's other half; a replay changes nothing."""
    authorize(caller, Permission.INFERENCE_HOST_EDIT)
    host = _existing_host(host_id, hosts)
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
    caller: Caller,
    hosts: InferenceHostRepository,
    backends: InferenceBackendRepository,
) -> None:
    """Remove the host outright — the irreversible operation 停用 exists to avoid.

    Refuses while any backend still hangs off the machine; the foreign key holds the same
    rule as the transaction's backstop.
    """
    authorize(caller, Permission.INFERENCE_HOST_DELETE)
    _existing_host(host_id, hosts)
    if backends.any_for_host(host_id):
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.INFERENCE_HOST_HAS_BACKENDS,
            host_id=str(host_id),
            actor_id=str(caller.user.id),
        )
    if not hosts.remove(host_id):
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
    """Read one host. Refuses `INFERENCE_HOST_NOT_FOUND` when it is gone."""
    authorize(caller, Permission.INFERENCE_HOST_VIEW)
    return _existing_host(host_id, hosts)


def list_hosts(
    *, caller: Caller, hosts: InferenceHostRepository, page: int, page_size: int
) -> tuple[list[InferenceHost], int]:
    """One page of hosts, newest first, with the unpaginated total (§5.15's envelope)."""
    authorize(caller, Permission.INFERENCE_HOST_VIEW)
    return hosts.page_of(page=page, page_size=page_size)


def _existing_host(host_id: UUID, hosts: InferenceHostRepository) -> InferenceHost:
    host = hosts.by_id(host_id)
    if host is None:
        refuse(_REFUSAL_EVENT, DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND, host_id=str(host_id))
    return host
