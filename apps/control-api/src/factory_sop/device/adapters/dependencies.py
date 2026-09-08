"""What a `device` route declares to reach the module's seams.

The repositories come from the request's one transaction (ADR-0002), the probe from the
process. Authentication is `auth`'s `Authenticated` dependency; authorization itself is a
use-case-layer decision, not a route dependency (§5.15).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.device.adapters.command_repository import PostgresPendingCommandRepository
from factory_sop.device.adapters.probe import UrllibConnectionProbe
from factory_sop.device.adapters.repository import (
    PostgresCameraRepository,
    PostgresConnectorRepository,
    PostgresInferenceBackendRepository,
    PostgresInferenceHostRepository,
    PostgresPointRepository,
    PostgresStationRepository,
)
from factory_sop.device.probing import ConnectionProbe
from factory_sop.device.repository import (
    CameraRepository,
    ConnectorRepository,
    InferenceBackendRepository,
    InferenceHostRepository,
    PendingCommandRepository,
    PointRepository,
    StationRepository,
)
from factory_sop.persistence import request_session


def hosts(session: Annotated[DatabaseSession, Depends(request_session)]) -> InferenceHostRepository:
    """`device_inference_host` on the request's transaction."""
    return PostgresInferenceHostRepository(session)


def backends(
    session: Annotated[DatabaseSession, Depends(request_session)],
) -> InferenceBackendRepository:
    """`device_inference_backend` on the request's transaction."""
    return PostgresInferenceBackendRepository(session)


def stations(
    session: Annotated[DatabaseSession, Depends(request_session)],
) -> StationRepository:
    """请求事务中的 `device_station`。"""
    return PostgresStationRepository(session)


def cameras(
    session: Annotated[DatabaseSession, Depends(request_session)],
) -> CameraRepository:
    """请求事务中的 `device_camera`。"""
    return PostgresCameraRepository(session)


def connectors(
    session: Annotated[DatabaseSession, Depends(request_session)],
) -> ConnectorRepository:
    """请求事务中的 `device_connector`。"""
    return PostgresConnectorRepository(session)


def points(
    session: Annotated[DatabaseSession, Depends(request_session)],
) -> PointRepository:
    """请求事务中的 `device_point`。"""
    return PostgresPointRepository(session)


def probe() -> ConnectionProbe:
    """The real network probe. Route tests replace it with a scripted stand-in."""
    return UrllibConnectionProbe()


def pending_commands(
    session: Annotated[DatabaseSession, Depends(request_session)],
) -> PendingCommandRepository:
    """`device_pending_command` on the request's transaction."""
    return PostgresPendingCommandRepository(session)
