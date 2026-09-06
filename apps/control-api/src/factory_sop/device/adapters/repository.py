"""The PostgreSQL side of `device`'s two repository seams.

No method commits. One request is one transaction, opened and committed by the HTTP adapter
layer (ADR-0002). Conditional writes and PostgreSQL constraint violations are translated here
so concurrent configuration changes surface as deterministic module refusals, not 500s.
"""

from __future__ import annotations

from typing import Any, NoReturn, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, exists, func, select, update
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.device.adapters.tables import (
    CameraRow,
    InferenceBackendRow,
    InferenceHostRow,
    StationRow,
)
from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.model import Camera, InferenceBackend, InferenceHost, Station

# What each unique-constraint violation means, named by the metadata convention
# (factory_sop/persistence.py). The backend foreign key has two meanings depending on which
# operation caused it, so its refusal is supplied by the call site.
_CONSTRAINT_REFUSALS: dict[str, DeviceRefusalCode] = {
    "uq_device_inference_host_name": DeviceRefusalCode.INFERENCE_HOST_NAME_TAKEN,
    "uq_device_inference_backend_host_id_base_url": (
        DeviceRefusalCode.INFERENCE_BACKEND_ENDPOINT_TAKEN
    ),
    "uq_device_station_code": DeviceRefusalCode.STATION_CODE_TAKEN,
}
_HOST_FOREIGN_KEY = "fk_device_inference_backend_host_id_device_inference_host"
_TRIGGERED_REFUSALS = {
    "device_backend_host_deactivated": DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED,
    "device_camera_host_backend_mismatch": DeviceRefusalCode.CAMERA_HOST_BACKEND_MISMATCH,
    "device_camera_host_deactivated": DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED,
    "device_camera_backend_deactivated": DeviceRefusalCode.INFERENCE_BACKEND_DEACTIVATED,
    "device_camera_station_deactivated": DeviceRefusalCode.STATION_DEACTIVATED,
    "device_camera_station_host_conflict": DeviceRefusalCode.CAMERA_STATION_HOST_CONFLICT,
    "device_camera_station_template_conflict": DeviceRefusalCode.CAMERA_STATION_TEMPLATE_CONFLICT,
}


def _refuse_constraint_violation(
    error: DatabaseError,
    *,
    foreign_key_to_host: DeviceRefusalCode | None = None,
    foreign_key_to_station: DeviceRefusalCode | None = None,
    foreign_key_to_backend: DeviceRefusalCode | None = None,
) -> NoReturn:
    """Translate a known constraint or trigger refusal, preserving unknown DB errors."""
    diagnostics = getattr(error.orig, "diag", None)
    constraint_name = getattr(diagnostics, "constraint_name", None)
    sqlstate = getattr(error.orig, "sqlstate", None)
    message = getattr(diagnostics, "message_primary", None)
    if sqlstate == "P0001" and message in _TRIGGERED_REFUSALS:
        raise DeviceRefusedError(_TRIGGERED_REFUSALS[message]) from error
    if constraint_name == _HOST_FOREIGN_KEY:
        if foreign_key_to_host is None:
            raise error
        raise DeviceRefusedError(foreign_key_to_host) from error
    if constraint_name == "fk_device_camera_station_id_device_station":
        if foreign_key_to_station is None:
            raise error
        raise DeviceRefusedError(foreign_key_to_station) from error
    if constraint_name == "fk_device_camera_host_id_device_inference_host":
        if foreign_key_to_host is None:
            raise error
        raise DeviceRefusedError(foreign_key_to_host) from error
    if constraint_name == "fk_device_camera_backend_id_device_inference_backend":
        if foreign_key_to_backend is None:
            raise error
        raise DeviceRefusedError(foreign_key_to_backend) from error
    refusal = _CONSTRAINT_REFUSALS.get(constraint_name or "")
    if refusal is None:
        raise error
    raise DeviceRefusedError(refusal) from error


class PostgresInferenceHostRepository:
    """`device_inference_host` through the request's session."""

    def __init__(self, session: DatabaseSession) -> None:
        self._session = session

    def add(self, host: InferenceHost) -> None:
        self._session.add(InferenceHostRow.from_domain(host))
        try:
            self._session.flush()
        except DatabaseError as error:
            _refuse_constraint_violation(error)

    def save(self, host: InferenceHost, *, expected_revision: int) -> None:
        values = {
            "name": host.name,
            "address": host.address,
            "mediamtx_address": host.mediamtx_address,
            "recording_window_seconds": host.recording_window_seconds,
            "disk_watermark_percent": host.disk_watermark_percent,
            "status": host.status,
            "revision": host.revision,
            "updated_by": host.updated_by,
            "updated_at": host.updated_at,
        }
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    update(InferenceHostRow)
                    .where(
                        InferenceHostRow.id == host.id,
                        InferenceHostRow.revision == expected_revision,
                    )
                    .values(**values)
                ),
            )
        except DatabaseError as error:
            _refuse_constraint_violation(error)
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.scalar(
                    select(InferenceHostRow)
                    .where(InferenceHostRow.id == host.id)
                    .execution_options(populate_existing=True)
                ),
            )

    def by_id(self, host_id: UUID) -> InferenceHost | None:
        row = self._session.get(InferenceHostRow, host_id)
        return row.to_domain() if row is not None else None

    def remove(self, host_id: UUID, *, expected_revision: int) -> bool:
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    delete(InferenceHostRow).where(
                        InferenceHostRow.id == host_id,
                        InferenceHostRow.revision == expected_revision,
                    )
                ),
            )
        except DatabaseError as error:
            # A backend row keeps the historical reference. This is the database backstop for
            # `delete_host`, which checks the same invariant through `any_for_host` first.
            _refuse_constraint_violation(
                error,
                foreign_key_to_host=DeviceRefusalCode.INFERENCE_HOST_HAS_BACKENDS,
            )
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.scalar(
                    select(InferenceHostRow)
                    .where(InferenceHostRow.id == host_id)
                    .execution_options(populate_existing=True)
                ),
            )
        return True

    def page_of(self, *, page: int, page_size: int) -> tuple[list[InferenceHost], int]:
        total = cast(
            "int", self._session.scalar(select(func.count()).select_from(InferenceHostRow))
        )
        rows = self._session.scalars(
            select(InferenceHostRow)
            .order_by(InferenceHostRow.created_at.desc(), InferenceHostRow.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [row.to_domain() for row in rows], total


class PostgresInferenceBackendRepository:
    """`device_inference_backend` through the request's session."""

    def __init__(self, session: DatabaseSession) -> None:
        self._session = session

    def add(self, backend: InferenceBackend) -> None:
        self._session.add(InferenceBackendRow.from_domain(backend))
        try:
            self._session.flush()
        except DatabaseError as error:
            _refuse_constraint_violation(
                error,
                foreign_key_to_host=DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND,
            )

    def save(self, backend: InferenceBackend, *, expected_revision: int) -> None:
        values = {
            "host_id": backend.host_id,
            "base_url": backend.base_url,
            "template_version_id": backend.template_version_id,
            "status": backend.status,
            "connection_state": backend.connection_state,
            "connection_checked_at": backend.connection_checked_at,
            "connection_detail": backend.connection_detail,
            "self_reported_model_ids": list(backend.self_reported_model_ids),
            "self_reported_at": backend.self_reported_at,
            "revision": backend.revision,
            "updated_by": backend.updated_by,
            "updated_at": backend.updated_at,
        }
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    update(InferenceBackendRow)
                    .where(
                        InferenceBackendRow.id == backend.id,
                        InferenceBackendRow.revision == expected_revision,
                    )
                    .values(**values)
                ),
            )
        except DatabaseError as error:
            _refuse_constraint_violation(error)
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.INFERENCE_BACKEND_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.get(InferenceBackendRow, backend.id),
            )

    def by_id(self, backend_id: UUID) -> InferenceBackend | None:
        row = self._session.get(InferenceBackendRow, backend_id)
        return row.to_domain() if row is not None else None

    def remove(self, backend_id: UUID, *, expected_revision: int) -> bool:
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    delete(InferenceBackendRow).where(
                        InferenceBackendRow.id == backend_id,
                        InferenceBackendRow.revision == expected_revision,
                    )
                ),
            )
        except DatabaseError as error:
            _refuse_constraint_violation(
                error,
                foreign_key_to_backend=DeviceRefusalCode.INFERENCE_BACKEND_HAS_CAMERAS,
            )
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.INFERENCE_BACKEND_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.get(InferenceBackendRow, backend_id),
            )
        return True

    def any_for_host(self, host_id: UUID) -> bool:
        return bool(
            self._session.scalar(select(exists().where(InferenceBackendRow.host_id == host_id)))
        )

    def page_of(
        self, *, page: int, page_size: int, host_id: UUID | None
    ) -> tuple[list[InferenceBackend], int]:
        query = select(func.count()).select_from(InferenceBackendRow)
        if host_id is not None:
            query = query.where(InferenceBackendRow.host_id == host_id)
        total = cast("int", self._session.scalar(query))

        listing = select(InferenceBackendRow)
        if host_id is not None:
            listing = listing.where(InferenceBackendRow.host_id == host_id)
        rows = self._session.scalars(
            listing.order_by(InferenceBackendRow.created_at.desc(), InferenceBackendRow.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [row.to_domain() for row in rows], total


class PostgresStationRepository:
    def __init__(self, session: DatabaseSession) -> None:
        self._session = session

    def add(self, station: Station) -> None:
        self._session.add(StationRow.from_domain(station))
        try:
            self._session.flush()
        except DatabaseError as error:
            _refuse_constraint_violation(error)

    def save(self, station: Station, *, expected_revision: int) -> None:
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    update(StationRow)
                    .where(StationRow.id == station.id, StationRow.revision == expected_revision)
                    .values(
                        code=station.code,
                        name=station.name,
                        tags=list(station.tags),
                        status=station.status,
                        revision=station.revision,
                        updated_by=station.updated_by,
                        updated_at=station.updated_at,
                    )
                ),
            )
        except DatabaseError as error:
            _refuse_constraint_violation(error)
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.STATION_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.get(StationRow, station.id),
            )

    def by_id(self, station_id: UUID) -> Station | None:
        row = self._session.get(StationRow, station_id)
        return row.to_domain() if row is not None else None

    def remove(self, station_id: UUID, *, expected_revision: int) -> bool:
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    delete(StationRow).where(
                        StationRow.id == station_id,
                        StationRow.revision == expected_revision,
                    )
                ),
            )
        except DatabaseError as error:
            _refuse_constraint_violation(
                error,
                foreign_key_to_station=DeviceRefusalCode.STATION_HAS_CAMERAS,
            )
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.STATION_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.get(StationRow, station_id),
            )
        return True

    def page_of(self, *, page: int, page_size: int) -> tuple[list[Station], int]:
        total = cast("int", self._session.scalar(select(func.count()).select_from(StationRow)))
        rows = self._session.scalars(
            select(StationRow)
            .order_by(StationRow.created_at.desc(), StationRow.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [row.to_domain() for row in rows], total


class PostgresCameraRepository:
    def __init__(self, session: DatabaseSession) -> None:
        self._session = session

    def add(self, camera: Camera) -> None:
        self._session.add(CameraRow.from_domain(camera))
        try:
            self._session.flush()
        except DatabaseError as error:
            _refuse_constraint_violation(
                error,
                foreign_key_to_host=DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND,
                foreign_key_to_station=DeviceRefusalCode.STATION_NOT_FOUND,
                foreign_key_to_backend=DeviceRefusalCode.INFERENCE_BACKEND_NOT_FOUND,
            )

    def save(self, camera: Camera, *, expected_revision: int) -> None:
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    update(CameraRow)
                    .where(CameraRow.id == camera.id, CameraRow.revision == expected_revision)
                    .values(
                        name=camera.name,
                        address=camera.address,
                        main_stream_path=camera.main_stream_path,
                        sub_stream_path=camera.sub_stream_path,
                        credentials_configured=camera.credentials_configured,
                        station_id=camera.station_id,
                        host_id=camera.host_id,
                        backend_id=camera.backend_id,
                        status=camera.status,
                        revision=camera.revision,
                        updated_by=camera.updated_by,
                        updated_at=camera.updated_at,
                    )
                ),
            )
        except DatabaseError as error:
            _refuse_constraint_violation(
                error,
                foreign_key_to_host=DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND,
                foreign_key_to_station=DeviceRefusalCode.STATION_NOT_FOUND,
                foreign_key_to_backend=DeviceRefusalCode.INFERENCE_BACKEND_NOT_FOUND,
            )
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.CAMERA_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.get(CameraRow, camera.id),
            )

    def by_id(self, camera_id: UUID) -> Camera | None:
        row = self._session.get(CameraRow, camera_id)
        return row.to_domain() if row is not None else None

    def remove(self, camera_id: UUID, *, expected_revision: int) -> bool:
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                delete(CameraRow).where(
                    CameraRow.id == camera_id,
                    CameraRow.revision == expected_revision,
                )
            ),
        )
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.CAMERA_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.get(CameraRow, camera_id),
            )
        return True

    def any_for_station(self, station_id: UUID) -> bool:
        return bool(
            self._session.scalar(select(exists().where(CameraRow.station_id == station_id)))
        )

    def for_station(self, station_id: UUID) -> list[Camera]:
        rows = self._session.scalars(
            select(CameraRow)
            .where(CameraRow.station_id == station_id)
            .order_by(CameraRow.created_at, CameraRow.id)
        ).all()
        return [row.to_domain() for row in rows]

    def page_of(
        self, *, page: int, page_size: int, station_id: UUID | None
    ) -> tuple[list[Camera], int]:
        query = select(func.count()).select_from(CameraRow)
        listing = select(CameraRow)
        if station_id is not None:
            query = query.where(CameraRow.station_id == station_id)
            listing = listing.where(CameraRow.station_id == station_id)
        total = cast("int", self._session.scalar(query))
        rows = self._session.scalars(
            listing.order_by(CameraRow.created_at.desc(), CameraRow.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [row.to_domain() for row in rows], total


def _refuse_lost_race(
    *,
    missing_refusal: DeviceRefusalCode,
    present_refusal: DeviceRefusalCode,
    row: InferenceHostRow | InferenceBackendRow | StationRow | CameraRow | None,
) -> NoReturn:
    """Say which way the conditional update lost: the row moved, or the row is gone."""
    if row is None:
        raise DeviceRefusedError(missing_refusal)
    raise DeviceRefusedError(present_refusal)
