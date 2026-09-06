"""The device repository translates only the trigger marker it owns."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.device.adapters.repository import PostgresInferenceHostRepository
from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError


class DriverError(Exception):
    """The subset of a DBAPI exception the PostgreSQL adapter reads."""

    def __init__(self, *, sqlstate: str, message: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.diag = SimpleNamespace(message_primary=message, constraint_name=None)


class FailingSession:
    """A database-boundary stand-in whose statement fails before a row is changed."""

    def __init__(self, error: DatabaseError) -> None:
        self.error = error

    def execute(self, statement: Any) -> NoReturn:  # noqa: ANN401 — SQLAlchemy accepts many statements
        raise self.error


def database_error(*, sqlstate: str, message: str) -> DatabaseError:
    return DatabaseError(
        "DELETE FROM device_inference_host", {}, DriverError(sqlstate=sqlstate, message=message)
    )


def repository_for(error: DatabaseError) -> PostgresInferenceHostRepository:
    return PostgresInferenceHostRepository(cast(DatabaseSession, FailingSession(error)))


def test_the_backend_deactivation_marker_becomes_a_device_refusal() -> None:
    error = database_error(sqlstate="P0001", message="device_backend_host_deactivated")

    with pytest.raises(DeviceRefusedError) as refused:
        repository_for(error).remove(cast(Any, "host-id"), expected_revision=1)

    assert refused.value.code is DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED


def test_an_unknown_database_error_is_not_hidden_as_a_device_refusal() -> None:
    error = database_error(sqlstate="XX000", message="unrelated database failure")

    with pytest.raises(DatabaseError) as raised:
        repository_for(error).remove(cast(Any, "host-id"), expected_revision=1)

    assert raised.value is error
