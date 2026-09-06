"""The two cross-cutting moves every `device` use case shares: refusing with diagnostics, and
the reversible 停用 transition.

They live here rather than being copied into each use-case module because they are one
behavior each, not two: every refusal in the module logs the same shape (stable event, the
`error_code`, the identifiers, never a credential) and every 停用/恢复 is the same write —
bump the revision, save against the revision the caller read, say so. A use case states which
resource it acts on and where its rows live; this module states how the module refuses and
transitions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import NoReturn
from uuid import UUID

from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.model import DeviceStatus, InferenceBackend, InferenceHost
from factory_sop.observability import get_logger

_logger = get_logger("device")


def refuse(event: str, code: DeviceRefusalCode, **context: str) -> NoReturn:
    """Log why the operation was refused — the identifiers, never a credential — and raise."""
    _logger.info(event, error_code=code.value, **context)
    raise DeviceRefusedError(code)


def set_status[ItemT: (InferenceHost, InferenceBackend)](
    item: ItemT,
    *,
    status: DeviceStatus,
    event: str,
    actor_id: UUID,
    now: datetime,
    context: dict[str, str],
    save: Callable[..., None],
) -> ItemT:
    """One reversible 停用 transition: bump the revision, save at the read revision, say so.

    `item` is the frozen record as read; `save` is the owning repository's save, so this
    module stays out of which store a host versus a backend lives in. `context` carries the
    record's identifier under its own event key (`host_id`, `backend_id`).
    """
    changed = replace(
        item,
        status=status,
        revision=item.revision + 1,
        updated_by=actor_id,
        updated_at=now,
    )
    save(changed, expected_revision=item.revision)
    _logger.info(event, **context, actor_id=str(actor_id))
    return changed
