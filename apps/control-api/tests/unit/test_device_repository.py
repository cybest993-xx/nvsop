"""The device repository translates only the trigger marker it owns."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, NoReturn, Protocol, cast
from uuid import UUID

import pytest
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.device.adapters.repository import (
    PostgresInferenceBackendRepository,
    PostgresInferenceHostRepository,
    PostgresStationRepository,
)
from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError


class DriverError(Exception):
    """The subset of a DBAPI exception the PostgreSQL adapter reads."""

    def __init__(self, *, sqlstate: str, message: str, constraint_name: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.diag = SimpleNamespace(message_primary=message, constraint_name=constraint_name)


class FailingSession:
    """A database-boundary stand-in whose statement fails before a row is changed."""

    def __init__(self, error: DatabaseError) -> None:
        self.error = error

    def execute(self, statement: Any) -> NoReturn:  # noqa: ANN401 — SQLAlchemy accepts many statements
        raise self.error


def database_error(
    *, sqlstate: str, message: str, constraint_name: str | None = None
) -> DatabaseError:
    return DatabaseError(
        "DELETE FROM device_inference_host",
        {},
        DriverError(
            sqlstate=sqlstate,
            message=message,
            constraint_name=constraint_name,
        ),
    )


class RemovableRepository(Protocol):
    def remove(self, resource_id: UUID, *, expected_revision: int) -> bool: ...


RepositoryFactory = Callable[[DatabaseSession], RemovableRepository]


def repository_for(
    error: DatabaseError,
    repository_type: RepositoryFactory = PostgresInferenceHostRepository,
) -> RemovableRepository:
    return repository_type(cast(DatabaseSession, FailingSession(error)))


def test_the_backend_deactivation_marker_becomes_a_device_refusal() -> None:
    error = database_error(sqlstate="P0001", message="device_backend_host_deactivated")

    with pytest.raises(DeviceRefusedError) as refused:
        repository_for(error).remove(cast(Any, "host-id"), expected_revision=1)

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED


@pytest.mark.parametrize(
    ("message", "code", "fields"),
    [
        (
            "device_camera_host_backend_mismatch",
            DeviceRefusalCode.CAMERA_HOST_BACKEND_MISMATCH,
            (
                ("host_id", "必须与推理后端所属推理机一致"),
                ("backend_id", "必须属于所选推理机"),
            ),
        ),
        (
            "device_camera_station_host_conflict",
            DeviceRefusalCode.CAMERA_STATION_HOST_CONFLICT,
            (("host_id", "同一工位的相机必须属于同一推理机"),),
        ),
        (
            "device_camera_station_template_conflict",
            DeviceRefusalCode.CAMERA_STATION_TEMPLATE_CONFLICT,
            (("backend_id", "该推理后端的模板必须与工位已有相机一致"),),
        ),
    ],
)
def test_topology_trigger_refusal_names_its_affected_fields(
    message: str, code: DeviceRefusalCode, fields: tuple[tuple[str, str], ...]
) -> None:
    error = database_error(sqlstate="P0001", message=message)

    with pytest.raises(DeviceRefusedError) as refused:
        repository_for(error).remove(cast(Any, "host-id"), expected_revision=1)

    assert refused.value.code is code
    assert [(item.field, item.message) for item in refused.value.field_errors] == list(fields)


@pytest.mark.parametrize(
    ("repository_type", "constraint_name", "code"),
    [
        (
            PostgresStationRepository,
            "fk_template_sop_template_station_id_device_station",
            DeviceRefusalCode.STATION_HAS_TEMPLATES,
        ),
        (
            PostgresStationRepository,
            "fk_template_station_binding_station_id_device_station",
            DeviceRefusalCode.STATION_HAS_TEMPLATE_BINDING,
        ),
        (
            PostgresStationRepository,
            "fk_template_configuration_report_station_id_device_station",
            DeviceRefusalCode.STATION_HAS_CONFIGURATION_REPORT,
        ),
        (
            PostgresInferenceBackendRepository,
            "fk_template_configuration_report_backend_id_device_infe_c4c5",
            DeviceRefusalCode.INFERENCE_BACKEND_HAS_CONFIGURATION_REPORT,
        ),
        (
            PostgresInferenceHostRepository,
            "fk_template_configuration_report_host_id_device_inference_host",
            DeviceRefusalCode.INFERENCE_HOST_HAS_CONFIGURATION_REPORT,
        ),
        (
            PostgresInferenceHostRepository,
            "fk_device_pending_command_host_id_device_inference_host",
            DeviceRefusalCode.INFERENCE_HOST_HAS_PENDING_COMMANDS,
        ),
    ],
)
def test_template_history_foreign_keys_become_stable_delete_refusals(
    repository_type: RepositoryFactory, constraint_name: str, code: DeviceRefusalCode
) -> None:
    error = database_error(
        sqlstate="23503",
        message="violates a template history foreign key",
        constraint_name=constraint_name,
    )

    with pytest.raises(DeviceRefusedError) as refused:
        repository_for(error, repository_type).remove(cast(Any, "parent-id"), expected_revision=1)

    assert refused.value.code is code


@pytest.mark.parametrize("sqlstate", ["XX000", "40P01"])
def test_an_unknown_database_error_is_not_hidden_as_a_device_refusal(sqlstate: str) -> None:
    error = database_error(sqlstate=sqlstate, message="unrelated database failure")

    with pytest.raises(DatabaseError) as raised:
        repository_for(error).remove(cast(Any, "host-id"), expected_revision=1)

    assert raised.value is error
