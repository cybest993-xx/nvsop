"""`device`'s host repository and the shared backend-history constraints against PostgreSQL.

The phase-one seam owns host CRUD. Backend rows are inserted only as a storage precondition here:
the backend API and its connection/probe behavior arrive in phase two. These tests prove the
foreign key prevents deleting a host with history, deactivation leaves that reference intact,
and the database trigger rejects a new backend reference to a deactivated host.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.device.adapters.repository import (
    PostgresInferenceHostRepository,
)
from factory_sop.device.adapters.tables import InferenceHostRow
from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.model import DeviceStatus, InferenceHost
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


def insert_backend_reference(session: DatabaseSession, host: InferenceHost) -> None:
    """Arrange a historical backend row without pretending phase two has an API."""
    session.execute(
        text(
            """
            INSERT INTO device_inference_backend (
                id, host_id, base_url, template_version_id, status, connection_state,
                connection_checked_at, connection_detail, self_reported_model_ids,
                self_reported_at, revision, created_by, updated_by, created_at, updated_at
            ) VALUES (
                :id, :host_id, :base_url, NULL, 'active', 'unverified',
                NULL, NULL, CAST(:model_ids AS jsonb), NULL, 1,
                :actor, :actor, :created_at, :created_at
            )
            """
        ),
        {
            "id": new_id(),
            "host_id": host.id,
            "base_url": "http://10.0.8.11:8000",
            "model_ids": "[]",
            "actor": host.created_by,
            "created_at": MONDAY_MORNING,
        },
    )


def test_a_host_round_trips_through_its_table(session: DatabaseSession) -> None:
    hosts = PostgresInferenceHostRepository(session)
    stored = a_host()
    hosts.add(stored)
    session.flush()
    session.expunge_all()

    assert hosts.by_id(stored.id) == stored


@pytest.mark.parametrize(
    ("recording_window_seconds", "disk_watermark_percent"),
    [(0, 85), (-1, 85), (7 * 24 * 3600, 0), (7 * 24 * 3600, 100)],
)
def test_postgres_rejects_an_invalid_host_recording_configuration(
    session: DatabaseSession, recording_window_seconds: int, disk_watermark_percent: int
) -> None:
    host = a_host()

    with pytest.raises(DatabaseError):
        session.execute(
            text(
                """
                INSERT INTO device_inference_host (
                    id, name, address, mediamtx_address, recording_window_seconds,
                    disk_watermark_percent, status, revision, created_by, updated_by,
                    created_at, updated_at
                ) VALUES (
                    :id, :name, :address, :mediamtx_address, :recording_window_seconds,
                    :disk_watermark_percent, 'active', 1, :actor, :actor, :created_at,
                    :updated_at
                )
                """
            ),
            {
                "id": host.id,
                "name": host.name,
                "address": host.address,
                "mediamtx_address": host.mediamtx_address,
                "recording_window_seconds": recording_window_seconds,
                "disk_watermark_percent": disk_watermark_percent,
                "actor": host.created_by,
                "created_at": host.created_at,
                "updated_at": host.updated_at,
            },
        )


def test_a_duplicate_host_name_is_refused_by_the_real_constraint(
    session: DatabaseSession,
) -> None:
    hosts = PostgresInferenceHostRepository(session)
    hosts.add(a_host())

    with pytest.raises(DeviceRefusedError) as refused:
        hosts.add(a_host(name="装配A线-推理机1"))

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_NAME_TAKEN


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


def test_a_delete_at_a_stale_revision_is_refused_not_blindly_written(
    session: DatabaseSession,
) -> None:
    hosts = PostgresInferenceHostRepository(session)
    stored = a_host()
    hosts.add(stored)
    session.flush()

    with pytest.raises(DeviceRefusedError) as refused:
        hosts.remove(stored.id, expected_revision=stored.revision + 1)

    assert refused.value.code is DeviceRefusalCode.STALE_REVISION
    assert hosts.by_id(stored.id) == stored


def test_save_reports_a_concurrently_deleted_cached_host_as_missing(
    session: DatabaseSession, engine: Engine
) -> None:
    stored = a_host()
    with DatabaseSession(engine) as setup:
        PostgresInferenceHostRepository(setup).add(stored)
        setup.commit()

    hosts = PostgresInferenceHostRepository(session)
    assert hosts.by_id(stored.id) == stored
    cached = session.get(InferenceHostRow, stored.id)
    assert cached is not None

    with DatabaseSession(engine) as concurrent:
        concurrent.execute(
            text("DELETE FROM device_inference_host WHERE id = :host_id"),
            {"host_id": stored.id},
        )
        concurrent.commit()

    with pytest.raises(DeviceRefusedError) as refused:
        hosts.save(replace(stored, address="10.0.8.99"), expected_revision=stored.revision)

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND


def test_remove_reports_a_concurrently_deleted_cached_host_as_missing(
    session: DatabaseSession, engine: Engine
) -> None:
    stored = a_host()
    with DatabaseSession(engine) as setup:
        PostgresInferenceHostRepository(setup).add(stored)
        setup.commit()

    hosts = PostgresInferenceHostRepository(session)
    assert hosts.by_id(stored.id) == stored
    cached = session.get(InferenceHostRow, stored.id)
    assert cached is not None

    with DatabaseSession(engine) as concurrent:
        concurrent.execute(
            text("DELETE FROM device_inference_host WHERE id = :host_id"),
            {"host_id": stored.id},
        )
        concurrent.commit()

    with pytest.raises(DeviceRefusedError) as refused:
        hosts.remove(stored.id, expected_revision=stored.revision)

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND


def test_a_host_with_backend_history_cannot_be_deleted(session: DatabaseSession) -> None:
    hosts = PostgresInferenceHostRepository(session)
    stored = a_host()
    hosts.add(stored)
    session.flush()
    insert_backend_reference(session, stored)

    with pytest.raises(DeviceRefusedError) as refused:
        hosts.remove(stored.id, expected_revision=stored.revision)

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_HAS_BACKENDS


def test_deactivation_preserves_the_backend_history_edge(session: DatabaseSession) -> None:
    hosts = PostgresInferenceHostRepository(session)
    stored = a_host()
    hosts.add(stored)
    session.flush()
    insert_backend_reference(session, stored)

    hosts.save(
        replace(stored, status=DeviceStatus.DEACTIVATED, revision=stored.revision + 1),
        expected_revision=stored.revision,
    )
    session.flush()

    reference = session.execute(
        text("SELECT host_id, base_url FROM device_inference_backend WHERE host_id = :host_id"),
        {"host_id": stored.id},
    ).one()
    assert reference.host_id == stored.id
    assert reference.base_url == "http://10.0.8.11:8000"


def test_database_trigger_rejects_a_new_backend_reference_to_a_deactivated_host(
    session: DatabaseSession,
) -> None:
    hosts = PostgresInferenceHostRepository(session)
    stored = a_host(status=DeviceStatus.DEACTIVATED)
    hosts.add(stored)
    session.flush()

    with pytest.raises(DatabaseError, match="device_backend_host_deactivated"):
        insert_backend_reference(session, stored)
