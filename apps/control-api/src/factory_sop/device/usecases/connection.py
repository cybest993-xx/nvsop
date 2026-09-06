"""The inference-backend connection-test use case.

The operation asks the probe seam once and records exactly what a real endpoint reported. It
is diagnostic configuration behavior, not a prerequisite for saving an endpoint: an untested
backend starts `unverified`, a reachable one becomes `success`, and every other probe outcome
becomes `failure`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.device.errors import DeviceRefusalCode
from factory_sop.device.model import ConnectionState, InferenceBackend
from factory_sop.device.probing import ConnectionProbe
from factory_sop.device.repository import InferenceBackendRepository
from factory_sop.device.usecases._transitions import refuse
from factory_sop.observability import get_logger

_logger = get_logger("device")
_REFUSAL_EVENT = "device.inference_backend.refused"


def test_backend_connection(
    *,
    backend_id: UUID,
    caller: Caller,
    now: datetime,
    probe: ConnectionProbe,
    backends: InferenceBackendRepository,
) -> InferenceBackend:
    """Ask the endpoint and persist its three-valued connection result.

    A successful report stores the endpoint's self-reported model identities. A failed report
    clears those identities: they are not evidence of what the endpoint currently serves. The
    revision is advanced like every other backend write, so a concurrent edit cannot be
    silently overwritten.
    """
    authorize(caller, Permission.INFERENCE_BACKEND_EDIT)
    backend = backends.by_id(backend_id)
    if backend is None:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.INFERENCE_BACKEND_NOT_FOUND,
            backend_id=str(backend_id),
            actor_id=str(caller.user.id),
        )

    report = probe.probe(base_url=backend.base_url)
    if report.status is ConnectionState.SUCCESS:
        tested = replace(
            backend,
            connection_state=ConnectionState.SUCCESS,
            connection_checked_at=now,
            connection_detail=None,
            self_reported_model_ids=report.model_ids,
            self_reported_at=now,
            revision=backend.revision + 1,
            updated_by=caller.user.id,
            updated_at=now,
        )
    else:
        tested = replace(
            backend,
            connection_state=ConnectionState.FAILURE,
            connection_checked_at=now,
            connection_detail=report.detail,
            self_reported_model_ids=(),
            self_reported_at=None,
            revision=backend.revision + 1,
            updated_by=caller.user.id,
            updated_at=now,
        )

    backends.save(tested, expected_revision=backend.revision)
    _logger.info(
        "device.inference_backend.connection_test.completed",
        backend_id=str(backend.id),
        host_id=str(backend.host_id),
        outcome=tested.connection_state.value,
        model_count=len(tested.self_reported_model_ids),
        detail=tested.connection_detail,
        actor_id=str(caller.user.id),
    )
    return tested
