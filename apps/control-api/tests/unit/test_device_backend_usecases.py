"""`device`'s inference-backend use cases, including the three-state connection test.

The topology constraint — a backend belongs to exactly one existing, active host — is
enforced here in the use case and, for the database half, in
`tests/integration/test_device_repository.py`. The connection test never fabricates an
outcome: whatever the record shows came from the probe seam, and an unreachable endpoint
lands as `failure`, never as a simulated success.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from auth_fakes import caller_holding
from device_fakes import FAKE_NOW, FakeInferenceBackends, FakeInferenceHosts

from factory_sop.auth.api import Permission
from factory_sop.auth.authorization import AuthorizationRefusedError
from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.model import ConnectionState, DeviceStatus
from factory_sop.device.usecases.backends import (
    backend_by_identifier,
    create_backend,
    deactivate_backend,
    delete_backend,
    edit_backend,
    list_backends,
    restore_backend,
)

LATER = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
EVEN_LATER = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
CALLER = caller_holding(
    Permission.INFERENCE_BACKEND_VIEW,
    Permission.INFERENCE_BACKEND_EDIT,
    Permission.INFERENCE_BACKEND_DELETE,
)


class VanishingBackendDelete(FakeInferenceBackends):
    """Simulate another transaction deleting the row between read and delete."""

    def remove(self, backend_id: UUID, *, expected_revision: int) -> bool:
        return False


def test_create_backend_hangs_off_an_existing_active_host_and_starts_unverified() -> None:
    hosts = FakeInferenceHosts()
    backends = FakeInferenceBackends()
    host = hosts.register(name="装配A线-推理机1")

    backend = create_backend(
        host_id=host.id,
        base_url="http://10.0.8.11:8000",
        caller=CALLER,
        now=FAKE_NOW,
        hosts=hosts,
        backends=backends,
    )

    assert backends.rows[backend.id] == backend
    assert backend.host_id == host.id
    assert backend.status is DeviceStatus.ACTIVE
    # Q31: configuration saves offline and reads 未验证 until a real test has run.
    assert backend.connection_state is ConnectionState.UNVERIFIED
    assert backend.self_reported_model_ids == ()


def test_create_backend_refuses_an_unsafe_endpoint_before_storage() -> None:
    hosts = FakeInferenceHosts()
    host = hosts.register(name="装配A线-推理机1")
    backends = FakeInferenceBackends()

    with pytest.raises(ValueError, match="device URLs cannot carry credentials"):
        create_backend(
            host_id=host.id,
            base_url="http://10.0.8.11:8000?access_token=fixture-marker",
            caller=CALLER,
            now=FAKE_NOW,
            hosts=hosts,
            backends=backends,
        )

    assert backends.rows == {}


def test_a_caller_without_the_backend_permission_is_refused_before_anything_happens() -> None:
    hosts = FakeInferenceHosts()
    backends = FakeInferenceBackends()

    with pytest.raises(AuthorizationRefusedError):
        create_backend(
            host_id=UUID(int=1),
            base_url="http://10.0.8.11:8000",
            caller=caller_holding(),
            now=FAKE_NOW,
            hosts=hosts,
            backends=backends,
        )

    assert backends.rows == {}


def test_a_host_permission_does_not_grant_backend_edits() -> None:
    hosts = FakeInferenceHosts()
    backends = FakeInferenceBackends()
    host = hosts.register(name="装配A线-推理机1")
    backend = backends.register(host_id=host.id, base_url="http://10.0.8.11:8000")

    with pytest.raises(AuthorizationRefusedError):
        edit_backend(
            backend_id=backend.id,
            host_id=host.id,
            base_url="http://10.0.8.11:8001",
            expected_revision=backend.revision,
            caller=caller_holding(Permission.INFERENCE_HOST_EDIT),
            now=LATER,
            hosts=hosts,
            backends=backends,
        )

    assert backends.rows[backend.id].base_url == "http://10.0.8.11:8000"


def test_creating_a_backend_for_a_missing_host_is_refused() -> None:
    with pytest.raises(DeviceRefusedError) as refused:
        create_backend(
            host_id=UUID(int=1),
            base_url="http://10.0.8.11:8000",
            caller=CALLER,
            now=FAKE_NOW,
            hosts=FakeInferenceHosts(),
            backends=FakeInferenceBackends(),
        )

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND


def test_creating_a_backend_on_a_deactivated_host_is_refused() -> None:
    hosts = FakeInferenceHosts()
    host = hosts.register(name="装配A线-推理机1", status=DeviceStatus.DEACTIVATED)

    with pytest.raises(DeviceRefusedError) as refused:
        create_backend(
            host_id=host.id,
            base_url="http://10.0.8.11:8000",
            caller=CALLER,
            now=FAKE_NOW,
            hosts=hosts,
            backends=FakeInferenceBackends(),
        )

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED


def test_creating_a_backend_on_a_taken_endpoint_of_the_same_host_is_refused() -> None:
    hosts = FakeInferenceHosts()
    backends = FakeInferenceBackends()
    host = hosts.register(name="装配A线-推理机1")
    backends.register(host_id=host.id, base_url="http://10.0.8.11:8000")

    with pytest.raises(DeviceRefusedError) as refused:
        create_backend(
            host_id=host.id,
            base_url="http://10.0.8.11:8000",
            caller=CALLER,
            now=FAKE_NOW,
            hosts=hosts,
            backends=backends,
        )

    assert refused.value.code is DeviceRefusalCode.INFERENCE_BACKEND_ENDPOINT_TAKEN


def test_edit_backend_moves_the_record_and_resets_stale_connection_facts() -> None:
    hosts = FakeInferenceHosts()
    backends = FakeInferenceBackends()
    host = hosts.register(name="装配A线-推理机1")
    backend = backends.register(
        host_id=host.id,
        base_url="http://10.0.8.11:8000",
        connection_state=ConnectionState.SUCCESS,
        connection_checked_at=FAKE_NOW,
        self_reported_model_ids=("ds_sop_model",),
        self_reported_at=FAKE_NOW,
    )

    edited = edit_backend(
        backend_id=backend.id,
        host_id=host.id,
        base_url="http://10.0.8.11:8001",
        expected_revision=backend.revision,
        caller=CALLER,
        now=LATER,
        hosts=hosts,
        backends=backends,
    )

    # The endpoint changed, so everything the old endpoint self-reported describes something
    # that is no longer this backend's configuration. Q31: it is 未验证 until tested again.
    assert edited.base_url == "http://10.0.8.11:8001"
    assert edited.connection_state is ConnectionState.UNVERIFIED
    assert edited.connection_checked_at is None
    assert edited.self_reported_model_ids == ()
    assert edited.self_reported_at is None
    assert edited.revision == 2


def test_edit_backend_that_keeps_the_endpoint_keeps_its_connection_facts() -> None:
    hosts = FakeInferenceHosts()
    backends = FakeInferenceBackends()
    host = hosts.register(name="装配A线-推理机1")
    backend = backends.register(
        host_id=host.id,
        base_url="http://10.0.8.11:8000",
        connection_state=ConnectionState.FAILURE,
        connection_checked_at=FAKE_NOW,
        connection_detail="connection refused",
    )

    edited = edit_backend(
        backend_id=backend.id,
        host_id=host.id,
        base_url="http://10.0.8.11:8000",
        expected_revision=backend.revision,
        caller=CALLER,
        now=LATER,
        hosts=hosts,
        backends=backends,
    )

    assert edited.connection_state is ConnectionState.FAILURE
    assert edited.connection_detail == "connection refused"
    assert edited.revision == 2


def test_moving_a_backend_onto_a_taken_endpoint_is_refused() -> None:
    hosts = FakeInferenceHosts()
    backends = FakeInferenceBackends()
    host = hosts.register(name="装配A线-推理机1")
    taken = backends.register(host_id=host.id, base_url="http://10.0.8.11:8001")
    backend = backends.register(host_id=host.id, base_url="http://10.0.8.11:8000")

    with pytest.raises(DeviceRefusedError) as refused:
        edit_backend(
            backend_id=backend.id,
            host_id=host.id,
            base_url="http://10.0.8.11:8001",
            expected_revision=backend.revision,
            caller=CALLER,
            now=LATER,
            hosts=hosts,
            backends=backends,
        )

    # Same placement pair, another row: the constraint holds on an edit, translated.
    assert refused.value.code is DeviceRefusalCode.INFERENCE_BACKEND_ENDPOINT_TAKEN
    assert backends.rows[backend.id].base_url == "http://10.0.8.11:8000"
    assert backends.rows[taken.id].base_url == "http://10.0.8.11:8001"


def test_moving_a_backend_to_another_host_retires_its_self_reported_facts() -> None:
    hosts = FakeInferenceHosts()
    backends = FakeInferenceBackends()
    host = hosts.register(name="装配A线-推理机1")
    other = hosts.register(name="装配B线-推理机2")
    backend = backends.register(
        host_id=host.id,
        base_url="http://10.0.8.11:8000",
        connection_state=ConnectionState.SUCCESS,
        connection_checked_at=FAKE_NOW,
        self_reported_model_ids=("ds_sop_model",),
        self_reported_at=FAKE_NOW,
    )

    moved = edit_backend(
        backend_id=backend.id,
        host_id=other.id,
        base_url="http://10.0.8.11:8000",
        expected_revision=backend.revision,
        caller=CALLER,
        now=LATER,
        hosts=hosts,
        backends=backends,
    )

    # §5.13 attributes the model identity to the host; a backend re-registered onto another
    # machine carries nothing the old machine self-reported, endpoint unchanged or not.
    assert moved.host_id == other.id
    assert moved.connection_state is ConnectionState.UNVERIFIED
    assert moved.self_reported_model_ids == ()


def test_editing_an_endpoint_on_a_deactivated_host_keeps_history_editable() -> None:
    hosts = FakeInferenceHosts()
    backends = FakeInferenceBackends()
    host = hosts.register(name="装配A线-推理机1", status=DeviceStatus.DEACTIVATED)
    backend = backends.register(host_id=host.id, base_url="http://10.0.8.11:8000")

    edited = edit_backend(
        backend_id=backend.id,
        host_id=host.id,
        base_url="http://10.0.8.11:8001",
        expected_revision=backend.revision,
        caller=CALLER,
        now=LATER,
        hosts=hosts,
        backends=backends,
    )

    assert edited.base_url == "http://10.0.8.11:8001"
    assert edited.connection_state is ConnectionState.UNVERIFIED
    assert hosts.rows[host.id].status is DeviceStatus.DEACTIVATED


def test_edit_backend_refuses_a_lost_revision_race() -> None:
    hosts = FakeInferenceHosts()
    backends = FakeInferenceBackends()
    host = hosts.register(name="装配A线-推理机1")
    backend = backends.register(host_id=host.id, base_url="http://10.0.8.11:8000")

    with pytest.raises(DeviceRefusedError) as refused:
        edit_backend(
            backend_id=backend.id,
            host_id=host.id,
            base_url="http://10.0.8.11:8000",
            expected_revision=backend.revision + 1,
            caller=CALLER,
            now=LATER,
            hosts=hosts,
            backends=backends,
        )

    assert refused.value.code is DeviceRefusalCode.STALE_REVISION
    assert backends.rows[backend.id] == backend


def test_edit_backend_refuses_a_move_to_a_missing_or_deactivated_host() -> None:
    hosts = FakeInferenceHosts()
    backends = FakeInferenceBackends()
    host = hosts.register(name="装配A线-推理机1")
    backend = backends.register(host_id=host.id, base_url="http://10.0.8.11:8000")
    deactivated = hosts.register(name="装配B线-推理机2", status=DeviceStatus.DEACTIVATED)

    with pytest.raises(DeviceRefusedError) as refused:
        edit_backend(
            backend_id=backend.id,
            host_id=UUID(int=1),
            base_url="http://10.0.8.11:8000",
            expected_revision=backend.revision,
            caller=CALLER,
            now=LATER,
            hosts=hosts,
            backends=backends,
        )
    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND

    with pytest.raises(DeviceRefusedError) as refused:
        edit_backend(
            backend_id=backend.id,
            host_id=deactivated.id,
            base_url="http://10.0.8.11:8000",
            expected_revision=backend.revision,
            caller=CALLER,
            now=LATER,
            hosts=hosts,
            backends=backends,
        )
    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED


def test_the_deactivation_lifecycle_of_a_backend_is_reversible_and_idempotent() -> None:
    hosts = FakeInferenceHosts()
    backends = FakeInferenceBackends()
    host = hosts.register(name="装配A线-推理机1")
    backend = backends.register(host_id=host.id, base_url="http://10.0.8.11:8000")

    deactivated = deactivate_backend(
        backend_id=backend.id,
        expected_revision=backend.revision,
        caller=CALLER,
        now=LATER,
        backends=backends,
    )
    replayed = deactivate_backend(
        backend_id=backend.id,
        expected_revision=deactivated.revision,
        caller=CALLER,
        now=LATER,
        backends=backends,
    )
    assert deactivated.status is DeviceStatus.DEACTIVATED
    assert replayed == deactivated
    assert backends.rows[backend.id].revision == 2

    restored = restore_backend(
        backend_id=backend.id,
        expected_revision=deactivated.revision,
        caller=CALLER,
        now=EVEN_LATER,
        hosts=hosts,
        backends=backends,
    )
    assert restored.status is DeviceStatus.ACTIVE
    assert backends.rows[backend.id].revision == 3


def test_restoring_a_backend_refuses_when_its_host_is_deactivated() -> None:
    hosts = FakeInferenceHosts()
    backends = FakeInferenceBackends()
    host = hosts.register(name="装配A线-推理机1", status=DeviceStatus.DEACTIVATED)
    backend = backends.register(
        host_id=host.id,
        base_url="http://10.0.8.11:8000",
        status=DeviceStatus.DEACTIVATED,
    )

    with pytest.raises(DeviceRefusedError) as refused:
        restore_backend(
            backend_id=backend.id,
            expected_revision=backend.revision,
            caller=CALLER,
            now=LATER,
            hosts=hosts,
            backends=backends,
        )

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED
    assert backends.rows[backend.id] == backend


def test_deleting_a_backend_removes_it_and_a_missing_one_is_refused() -> None:
    hosts = FakeInferenceHosts()
    backends = FakeInferenceBackends()
    host = hosts.register(name="装配A线-推理机1")
    backend = backends.register(host_id=host.id, base_url="http://10.0.8.11:8000")

    delete_backend(
        backend_id=backend.id,
        expected_revision=backend.revision,
        caller=CALLER,
        backends=backends,
    )
    assert backends.by_id(backend.id) is None

    with pytest.raises(DeviceRefusedError) as refused:
        delete_backend(
            backend_id=backend.id,
            expected_revision=backend.revision,
            caller=CALLER,
            backends=backends,
        )

    assert refused.value.code is DeviceRefusalCode.INFERENCE_BACKEND_NOT_FOUND


def test_a_delete_race_reports_not_found_instead_of_logging_success() -> None:
    hosts = FakeInferenceHosts()
    backends = VanishingBackendDelete()
    host = hosts.register(name="装配A线-推理机1")
    backend = backends.register(host_id=host.id, base_url="http://10.0.8.11:8000")

    with pytest.raises(DeviceRefusedError) as refused:
        delete_backend(
            backend_id=backend.id,
            expected_revision=backend.revision,
            caller=CALLER,
            backends=backends,
        )

    assert refused.value.code is DeviceRefusalCode.INFERENCE_BACKEND_NOT_FOUND
    assert backends.by_id(backend.id) == backend


def test_reading_a_backend_that_is_gone_is_refused() -> None:
    with pytest.raises(DeviceRefusedError) as refused:
        backend_by_identifier(
            backend_id=UUID(int=1), caller=CALLER, backends=FakeInferenceBackends()
        )

    assert refused.value.code is DeviceRefusalCode.INFERENCE_BACKEND_NOT_FOUND


def test_listing_backends_filters_by_host_and_pages_with_the_total() -> None:
    hosts = FakeInferenceHosts()
    backends = FakeInferenceBackends()
    host_a = hosts.register(name="装配A线-推理机1")
    host_b = hosts.register(name="装配B线-推理机2")
    a1 = backends.register(host_id=host_a.id, base_url="http://10.0.8.11:8000", created_at=FAKE_NOW)
    a2 = backends.register(host_id=host_a.id, base_url="http://10.0.8.11:8001", created_at=LATER)
    backends.register(host_id=host_b.id, base_url="http://10.0.8.12:8000")

    items, total = list_backends(
        caller=CALLER, backends=backends, page=1, page_size=10, host_id=host_a.id
    )
    assert (items, total) == ([a2, a1], 2)

    items, total = list_backends(
        caller=CALLER, backends=backends, page=1, page_size=10, host_id=None
    )
    assert total == 3
