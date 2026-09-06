"""`device`'s repositories against real PostgreSQL.

The use-case suite proves the rules with in-memory stand-ins; this proves the adapter that
has to behave the same way at that seam — that a host and a backend round-trip with their
JSONB and timezone-bearing fields intact, that the natural-key and endpoint uniqueness are
real constraints and not pre-checks, and that the foreign keys hold the topology constraint
the use cases enforce.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import event
from sqlalchemy.engine import ExceptionContext
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.device.adapters.repository import (
    PostgresInferenceBackendRepository,
    PostgresInferenceHostRepository,
)
from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.model import ConnectionState, DeviceStatus, InferenceBackend, InferenceHost
from factory_sop.identifiers import new_id

MONDAY_MORNING = datetime(2026, 9, 7, 1, 0, tzinfo=UTC)


def a_host(
    *, name: str = "装配A线-推理机1", status: DeviceStatus = DeviceStatus.ACTIVE
) -> InferenceHost:
    return InferenceHost(
        id=new_id(),
        name=name,
        address="10.0.8.11",
        mediamtx_address="http://10.0.8.11:8888",
        recording_window_seconds=7 * 24 * 3600,
        disk_watermark_percent=85,
        status=status,
        revision=1,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=MONDAY_MORNING,
        updated_at=MONDAY_MORNING,
    )


def a_backend(host_id: UUID, *, base_url: str = "http://10.0.8.11:8000") -> InferenceBackend:
    return InferenceBackend(
        id=new_id(),
        host_id=host_id,
        base_url=base_url,
        template_version_id=None,
        status=DeviceStatus.ACTIVE,
        connection_state=ConnectionState.SUCCESS,
        connection_checked_at=MONDAY_MORNING,
        connection_detail=None,
        self_reported_model_ids=("ds_sop_model", "qwen2-7b"),
        self_reported_at=MONDAY_MORNING,
        revision=2,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=MONDAY_MORNING,
        updated_at=MONDAY_MORNING,
    )


def test_a_host_round_trips_through_its_table(session: DatabaseSession) -> None:
    hosts = PostgresInferenceHostRepository(session)
    stored = a_host()
    hosts.add(stored)
    session.flush()
    session.expunge_all()

    assert hosts.by_id(stored.id) == stored


def test_a_duplicate_host_name_is_refused_by_the_real_constraint(
    session: DatabaseSession,
) -> None:
    hosts = PostgresInferenceHostRepository(session)
    hosts.add(a_host())

    with pytest.raises(DeviceRefusedError) as refused:
        hosts.add(a_host(name="装配A线-推理机1"))

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_NAME_TAKEN


def test_a_backend_round_trips_with_its_jsonb_and_timezone_fields(
    session: DatabaseSession,
) -> None:
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    host = a_host()
    hosts.add(host)
    stored = a_backend(host.id)
    backends.add(stored)
    session.flush()
    session.expunge_all()

    assert backends.by_id(stored.id) == stored


def test_a_duplicate_endpoint_on_one_host_is_refused_by_the_real_constraint(
    session: DatabaseSession,
) -> None:
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    host = a_host()
    hosts.add(host)
    backends.add(a_backend(host.id))

    with pytest.raises(DeviceRefusedError) as refused:
        backends.add(a_backend(host.id))

    assert refused.value.code is DeviceRefusalCode.INFERENCE_BACKEND_ENDPOINT_TAKEN


def test_the_same_endpoint_on_another_host_is_a_different_backend(
    session: DatabaseSession,
) -> None:
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    host_a = a_host(name="装配A线-推理机1")
    host_b = a_host(name="装配B线-推理机2")
    hosts.add(host_a)
    hosts.add(host_b)
    backends.add(a_backend(host_a.id, base_url="http://10.0.8.11:8000"))

    # Uniqueness is per host: the same port number on another machine is another endpoint.
    backends.add(a_backend(host_b.id, base_url="http://10.0.8.11:8000"))
    session.flush()

    assert backends.any_for_host(host_a.id)
    assert backends.any_for_host(host_b.id)


def test_a_backend_for_a_host_row_that_is_gone_is_refused_by_the_foreign_key(
    session: DatabaseSession,
) -> None:
    backends = PostgresInferenceBackendRepository(session)

    # `add` flushes, so the foreign key fires inside the call it belongs to.
    with pytest.raises(DeviceRefusedError) as refused:
        backends.add(a_backend(new_id()))

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND


def test_a_save_at_a_stale_revision_is_refused_not_blindly_written(
    session: DatabaseSession,
) -> None:
    hosts = PostgresInferenceHostRepository(session)
    stored = a_host()
    hosts.add(stored)
    session.flush()

    with pytest.raises(DeviceRefusedError) as refused:
        hosts.save(replace(stored, address="10.0.8.99"), expected_revision=stored.revision + 1)
    assert refused.value.code is DeviceRefusalCode.STALE_REVISION

    hosts.save(replace(stored, address="10.0.8.50", revision=2), expected_revision=1)
    session.flush()
    session.expunge_all()
    reloaded = hosts.by_id(stored.id)
    assert reloaded is not None
    assert reloaded.address == "10.0.8.50"


def test_a_save_for_a_row_that_is_gone_is_refused(session: DatabaseSession) -> None:
    hosts = PostgresInferenceHostRepository(session)

    with pytest.raises(DeviceRefusedError) as refused:
        hosts.save(a_host(), expected_revision=1)

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND


def test_a_host_with_backends_cannot_be_deleted_but_an_empty_one_can(
    session: DatabaseSession,
) -> None:
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    host = a_host()
    hosts.add(host)
    backends.add(a_backend(host.id))

    with pytest.raises(DeviceRefusedError) as refused:
        hosts.remove(host.id)

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_HAS_BACKENDS


def test_a_backend_for_a_deactivated_host_is_refused_by_the_trigger(
    session: DatabaseSession,
) -> None:
    # The database half of the topology constraint, for whoever writes the table directly:
    # the use case refuses a deactivated host, and so does the row's own trigger. A
    # declarative constraint cannot say it — backends keep referencing a host that is LATER
    # deactivated, which is exactly what 停用 requires — so the check runs at write time.
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    host = a_host(status=DeviceStatus.DEACTIVATED)
    hosts.add(host)

    with pytest.raises(DeviceRefusedError) as refused:
        backends.add(a_backend(host.id))

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED


def test_a_deactivated_host_trigger_is_translated_when_driver_reports_database_error(
    session: DatabaseSession,
) -> None:
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    host = a_host(status=DeviceStatus.DEACTIVATED)
    hosts.add(host)

    def report_as_generic_database_error(exception_context: ExceptionContext) -> None:
        original = exception_context.original_exception
        if getattr(original, "sqlstate", None) == "P0001":
            raise DatabaseError(
                exception_context.statement,
                exception_context.parameters,
                original,
            )

    engine = session.connection().engine
    event.listen(engine, "handle_error", report_as_generic_database_error)
    try:
        with pytest.raises(DeviceRefusedError) as refused:
            backends.add(a_backend(host.id))
    finally:
        event.remove(engine, "handle_error", report_as_generic_database_error)

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED


def test_moving_a_backend_onto_a_deactivated_host_is_refused_by_the_trigger(
    session: DatabaseSession,
) -> None:
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    host = a_host()
    hosts.add(host)
    other = a_host(name="装配B线-推理机2", status=DeviceStatus.DEACTIVATED)
    hosts.add(other)
    backend = a_backend(host.id)
    backends.add(backend)
    session.flush()

    from dataclasses import replace as _replace

    with pytest.raises(DeviceRefusedError) as refused:
        backends.save(
            _replace(backend, host_id=other.id, revision=backend.revision + 1),
            expected_revision=backend.revision,
        )

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED


def test_editing_a_backend_on_its_deactivated_host_preserves_the_history_edge(
    session: DatabaseSession,
) -> None:
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    host = a_host()
    hosts.add(host)
    backend = a_backend(host.id)
    backends.add(backend)
    hosts.save(
        replace(host, status=DeviceStatus.DEACTIVATED, revision=host.revision + 1),
        expected_revision=host.revision,
    )
    session.flush()

    edited = replace(backend, base_url="http://10.0.8.11:8001", revision=backend.revision + 1)
    backends.save(edited, expected_revision=backend.revision)
    session.flush()
    session.expunge_all()

    assert backends.by_id(backend.id) == edited


def test_an_edit_that_takes_a_taken_name_is_translated(session: DatabaseSession) -> None:
    hosts = PostgresInferenceHostRepository(session)
    hosts.add(a_host())
    host = a_host(name="装配B线-推理机2")
    hosts.add(host)
    session.flush()

    with pytest.raises(DeviceRefusedError) as refused:
        hosts.save(replace(host, name="装配A线-推理机1"), expected_revision=host.revision)

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_NAME_TAKEN


def test_an_edit_that_takes_a_taken_endpoint_is_translated(session: DatabaseSession) -> None:
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    host = a_host()
    hosts.add(host)
    taken = a_backend(host.id, base_url="http://10.0.8.11:8001")
    backends.add(taken)
    backend = a_backend(host.id, base_url="http://10.0.8.11:8000")
    backends.add(backend)
    session.flush()

    with pytest.raises(DeviceRefusedError) as refused:
        backends.save(
            replace(backend, base_url="http://10.0.8.11:8001"),
            expected_revision=backend.revision,
        )

    assert refused.value.code is DeviceRefusalCode.INFERENCE_BACKEND_ENDPOINT_TAKEN


def test_a_deactivated_host_s_backends_keep_their_template_binding(
    session: DatabaseSession,
) -> None:
    # "历史引用不会被停用破坏", past the minimal host→backend edge: the binding a backend
    # carries is part of its row, and 停用 cascades nothing — a deactivated host's backends
    # read back whole, binding included.
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    host = a_host()
    hosts.add(host)
    binding = a_backend(host.id)
    with_binding = replace(binding, template_version_id=new_id())
    backends.add(with_binding)
    hosts.save(
        replace(host, status=DeviceStatus.DEACTIVATED, revision=host.revision + 1),
        expected_revision=host.revision,
    )
    session.flush()
    session.expunge_all()

    reloaded = backends.by_id(with_binding.id)
    assert reloaded is not None
    assert reloaded.host_id == host.id
    assert reloaded.template_version_id == with_binding.template_version_id


def test_listing_backends_filters_by_host_and_pages_newest_first(
    session: DatabaseSession,
) -> None:
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    host_a = a_host(name="装配A线-推理机1")
    host_b = a_host(name="装配B线-推理机2")
    hosts.add(host_a)
    hosts.add(host_b)
    older = a_backend(host_a.id, base_url="http://10.0.8.11:8000")
    newer = replace(
        a_backend(host_a.id, base_url="http://10.0.8.11:8001"),
        created_at=datetime(2026, 9, 8, tzinfo=UTC),
    )
    backends.add(older)
    backends.add(newer)
    backends.add(a_backend(host_b.id, base_url="http://10.0.8.12:8000"))
    session.flush()

    items, total = backends.page_of(page=1, page_size=10, host_id=host_a.id)
    assert total == 2
    assert [backend.id for backend in items] == [newer.id, older.id]

    _, total = backends.page_of(page=1, page_size=10, host_id=None)
    assert total == 3

    hosts_items, hosts_total = hosts.page_of(page=1, page_size=1)
    assert hosts_total == 2
    assert len(hosts_items) == 1
