"""`device`'s inference-host use cases over in-memory stores.

What the suite decides without a database: the 停用 lifecycle is reversible and idempotent,
a lost revision race refuses rather than overwrites, and deleting a host that still carries
a backend is refused — the topology constraint enforced in the use case, whose database half
`tests/integration/test_device_repository.py` proves.
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
from factory_sop.device.model import DeviceStatus
from factory_sop.device.usecases.hosts import (
    create_host,
    deactivate_host,
    delete_host,
    edit_host,
    host_by_identifier,
    list_hosts,
    restore_host,
)

LATER = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
CALLER = caller_holding(
    Permission.INFERENCE_HOST_VIEW, Permission.INFERENCE_HOST_EDIT, Permission.INFERENCE_HOST_DELETE
)


class VanishingHostDelete(FakeInferenceHosts):
    """Simulate another transaction deleting the row between read and delete."""

    def remove(self, host_id: UUID) -> bool:
        return False


def test_create_host_stores_an_active_host_with_its_actor_and_clock() -> None:
    hosts = FakeInferenceHosts()

    host = create_host(
        name="装配A线-推理机1",
        address="10.0.8.11",
        mediamtx_address="http://10.0.8.11:8888",
        recording_window_seconds=7 * 24 * 3600,
        disk_watermark_percent=85,
        caller=CALLER,
        now=FAKE_NOW,
        hosts=hosts,
    )

    assert hosts.rows[host.id] == host
    assert host.status is DeviceStatus.ACTIVE
    assert host.revision == 1
    assert host.created_by == CALLER.user.id
    assert host.created_at == FAKE_NOW


def test_create_host_refuses_an_unsafe_media_endpoint_before_storage() -> None:
    hosts = FakeInferenceHosts()

    with pytest.raises(ValueError, match="device URLs cannot carry credentials"):
        create_host(
            name="装配A线-推理机1",
            address="10.0.8.11",
            mediamtx_address="http://10.0.8.11:8888?access_token=fixture-marker",
            recording_window_seconds=7 * 24 * 3600,
            disk_watermark_percent=85,
            caller=CALLER,
            now=FAKE_NOW,
            hosts=hosts,
        )

    assert hosts.rows == {}


def test_a_caller_without_the_host_permission_is_refused_before_anything_happens() -> None:
    hosts = FakeInferenceHosts()

    with pytest.raises(AuthorizationRefusedError):
        create_host(
            name="装配A线-推理机1",
            address="10.0.8.11",
            mediamtx_address=None,
            recording_window_seconds=7 * 24 * 3600,
            disk_watermark_percent=85,
            caller=caller_holding(),
            now=FAKE_NOW,
            hosts=hosts,
        )

    # The refusal is the whole effect: a denied caller writes nothing, not even a partial row.
    assert hosts.rows == {}


def test_a_backend_permission_does_not_grant_host_edits() -> None:
    hosts = FakeInferenceHosts()
    host = hosts.register(name="装配A线-推理机1")

    with pytest.raises(AuthorizationRefusedError):
        deactivate_host(
            host_id=host.id,
            caller=caller_holding(Permission.INFERENCE_BACKEND_EDIT),
            now=LATER,
            hosts=hosts,
        )

    # Holding the other resource's edit says nothing about this one: the sets are flat.
    assert hosts.rows[host.id].status is DeviceStatus.ACTIVE


def test_creating_a_host_with_a_taken_name_is_refused_by_the_natural_key() -> None:
    hosts = FakeInferenceHosts()
    hosts.register(name="装配A线-推理机1")

    with pytest.raises(DeviceRefusedError) as refused:
        create_host(
            name="装配A线-推理机1",
            address="10.0.8.12",
            mediamtx_address=None,
            recording_window_seconds=7 * 24 * 3600,
            disk_watermark_percent=85,
            caller=CALLER,
            now=FAKE_NOW,
            hosts=hosts,
        )

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_NAME_TAKEN


def test_edit_host_replaces_the_whole_configuration_and_bumps_the_revision() -> None:
    hosts = FakeInferenceHosts()
    host = hosts.register(name="装配A线-推理机1")

    edited = edit_host(
        host_id=host.id,
        name="装配A线-推理机1A",
        address="10.0.8.99",
        mediamtx_address=None,
        recording_window_seconds=3 * 24 * 3600,
        disk_watermark_percent=90,
        expected_revision=host.revision,
        caller=CALLER,
        now=LATER,
        hosts=hosts,
    )

    assert hosts.rows[host.id] == edited
    assert (edited.name, edited.address, edited.mediamtx_address) == (
        "装配A线-推理机1A",
        "10.0.8.99",
        None,
    )
    assert (edited.recording_window_seconds, edited.disk_watermark_percent) == (3 * 24 * 3600, 90)
    assert edited.revision == 2
    assert (edited.updated_by, edited.updated_at) == (CALLER.user.id, LATER)


def test_edit_host_refuses_a_lost_revision_race_instead_of_overwriting() -> None:
    hosts = FakeInferenceHosts()
    host = hosts.register(name="装配A线-推理机1")

    with pytest.raises(DeviceRefusedError) as refused:
        edit_host(
            host_id=host.id,
            name="装配A线-推理机1",
            address="10.0.8.99",
            mediamtx_address=None,
            recording_window_seconds=7 * 24 * 3600,
            disk_watermark_percent=85,
            expected_revision=host.revision + 1,
            caller=CALLER,
            now=LATER,
            hosts=hosts,
        )

    assert refused.value.code is DeviceRefusalCode.STALE_REVISION
    assert hosts.rows[host.id] == host


def test_edit_host_refuses_a_host_that_is_gone() -> None:
    with pytest.raises(DeviceRefusedError) as refused:
        edit_host(
            host_id=UUID(int=1),
            name="装配A线-推理机1",
            address="10.0.8.99",
            mediamtx_address=None,
            recording_window_seconds=7 * 24 * 3600,
            disk_watermark_percent=85,
            expected_revision=1,
            caller=CALLER,
            now=LATER,
            hosts=FakeInferenceHosts(),
        )

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND


def test_editing_a_host_into_a_taken_name_is_refused_not_a_500() -> None:
    hosts = FakeInferenceHosts()
    hosts.register(name="装配B线-推理机2")
    host = hosts.register(name="装配A线-推理机1")

    with pytest.raises(DeviceRefusedError) as refused:
        edit_host(
            host_id=host.id,
            name="装配B线-推理机2",
            address=host.address,
            mediamtx_address=None,
            recording_window_seconds=host.recording_window_seconds,
            disk_watermark_percent=host.disk_watermark_percent,
            expected_revision=host.revision,
            caller=CALLER,
            now=LATER,
            hosts=hosts,
        )

    # The unique constraint holds on an edit too, and the adapter translates it — the
    # caller sees the same 409 a duplicate creation gets.
    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_NAME_TAKEN


def test_deactivating_a_host_is_reversible_and_a_replay_changes_nothing() -> None:
    hosts = FakeInferenceHosts()
    host = hosts.register(name="装配A线-推理机1")

    deactivated = deactivate_host(host_id=host.id, caller=CALLER, now=LATER, hosts=hosts)
    replayed = deactivate_host(host_id=host.id, caller=CALLER, now=LATER, hosts=hosts)

    assert deactivated.status is DeviceStatus.DEACTIVATED
    assert deactivated.revision == 2
    # A second 停用 is a retry or a double click, not an edit: nothing about the record moves.
    assert replayed == deactivated
    assert hosts.rows[host.id].revision == 2

    restored = restore_host(host_id=host.id, caller=CALLER, now=LATER, hosts=hosts)
    assert restored.status is DeviceStatus.ACTIVE
    assert hosts.rows[host.id].revision == 3


def test_deactivating_a_host_that_is_gone_is_refused() -> None:
    with pytest.raises(DeviceRefusedError) as refused:
        deactivate_host(host_id=UUID(int=1), caller=CALLER, now=LATER, hosts=FakeInferenceHosts())

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND


def test_deleting_a_host_that_still_carries_a_backend_is_refused() -> None:
    hosts = FakeInferenceHosts()
    backends = FakeInferenceBackends()
    host = hosts.register(name="装配A线-推理机1")
    backends.register(host_id=host.id, base_url="http://10.0.8.11:8000")

    with pytest.raises(DeviceRefusedError) as refused:
        delete_host(host_id=host.id, caller=CALLER, hosts=hosts, backends=backends)

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_HAS_BACKENDS
    assert hosts.rows[host.id] == host


def test_deleting_an_empty_host_removes_it_and_a_missing_one_is_refused() -> None:
    hosts = FakeInferenceHosts()
    host = hosts.register(name="装配A线-推理机1")

    delete_host(host_id=host.id, caller=CALLER, hosts=hosts, backends=FakeInferenceBackends())
    assert hosts.by_id(host.id) is None

    with pytest.raises(DeviceRefusedError) as refused:
        delete_host(host_id=host.id, caller=CALLER, hosts=hosts, backends=FakeInferenceBackends())
    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND


def test_reading_a_host_that_is_gone_is_refused() -> None:
    with pytest.raises(DeviceRefusedError) as refused:
        host_by_identifier(host_id=UUID(int=1), caller=CALLER, hosts=FakeInferenceHosts())

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND


def test_listing_hosts_is_newest_first_and_paged_with_the_total() -> None:
    hosts = FakeInferenceHosts()
    oldest = hosts.register(name="推理机-1", created_at=datetime(2026, 9, 1, tzinfo=UTC))
    middle = hosts.register(name="推理机-2", created_at=datetime(2026, 9, 2, tzinfo=UTC))
    newest = hosts.register(name="推理机-3", created_at=datetime(2026, 9, 3, tzinfo=UTC))

    items, total = list_hosts(caller=CALLER, hosts=hosts, page=1, page_size=2)
    assert (items, total) == ([newest, middle], 3)

    items, total = list_hosts(caller=CALLER, hosts=hosts, page=2, page_size=2)
    assert (items, total) == ([oldest], 3)


def test_a_delete_race_reports_not_found_instead_of_logging_success() -> None:
    hosts = VanishingHostDelete()
    host = hosts.register(name="装配A线-推理机1")

    with pytest.raises(DeviceRefusedError) as refused:
        delete_host(host_id=host.id, caller=CALLER, hosts=hosts, backends=FakeInferenceBackends())

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND
    assert hosts.by_id(host.id) == host


def test_an_edited_host_keeps_its_deactivated_status_until_restored() -> None:
    hosts = FakeInferenceHosts()
    host = hosts.register(name="装配A线-推理机1", status=DeviceStatus.DEACTIVATED)

    edited = edit_host(
        host_id=host.id,
        name="装配A线-推理机1",
        address="10.0.8.50",
        mediamtx_address=None,
        recording_window_seconds=7 * 24 * 3600,
        disk_watermark_percent=85,
        expected_revision=host.revision,
        caller=CALLER,
        now=LATER,
        hosts=hosts,
    )

    # 停用 means "no longer takes part in new bindings and operation", not "frozen": an
    # operator fixes the address of a decommissioned machine before restoring it.
    assert edited.status is DeviceStatus.DEACTIVATED
    assert edited.address == "10.0.8.50"
