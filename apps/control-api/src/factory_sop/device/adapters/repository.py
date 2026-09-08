"""The PostgreSQL side of `device`'s two repository seams.

No method commits. One request is one transaction, opened and committed by the HTTP adapter
layer (ADR-0002). Conditional writes and PostgreSQL constraint violations are translated here
so concurrent configuration changes surface as deterministic module refusals, not 500s.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, exists, func, insert, inspect, select, update
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.device.adapters.repository_support import (
    refuse_constraint_violation as _refuse_constraint_violation,
)
from factory_sop.device.adapters.repository_support import (
    refuse_lost_race as _refuse_lost_race,
)
from factory_sop.device.adapters.tables import (
    CameraRow,
    ConnectorRow,
    InferenceBackendRow,
    InferenceHostRow,
    PointRow,
    StationRow,
)
from factory_sop.device.errors import DeviceRefusalCode
from factory_sop.device.model import (
    Camera,
    Connector,
    InferenceBackend,
    InferenceHost,
    Point,
    Station,
)
from nvsop_contracts import capability_to_wire

__all__ = [
    "PostgresCameraRepository",
    "PostgresConnectorRepository",
    "PostgresInferenceBackendRepository",
    "PostgresInferenceHostRepository",
    "PostgresPointRepository",
    "PostgresStationRepository",
]


_HOST_BASE_COLUMNS = (
    "id",
    "name",
    "address",
    "mediamtx_address",
    "recording_window_seconds",
    "disk_watermark_percent",
    "status",
    "revision",
    "created_by",
    "updated_by",
    "created_at",
    "updated_at",
)


def _host_values(host: InferenceHost, *, include_credential: bool) -> dict[str, object]:
    values: dict[str, object] = {
        "id": host.id,
        "name": host.name,
        "address": host.address,
        "mediamtx_address": host.mediamtx_address,
        "recording_window_seconds": host.recording_window_seconds,
        "disk_watermark_percent": host.disk_watermark_percent,
        "status": host.status,
        "revision": host.revision,
        "created_by": host.created_by,
        "updated_by": host.updated_by,
        "created_at": host.created_at,
        "updated_at": host.updated_at,
    }
    if include_credential:
        values["credential_hash"] = host.credential_hash
    return values


def _host_from_values(values: Mapping[str, Any]) -> InferenceHost:
    return InferenceHost(
        id=values["id"],
        name=values["name"],
        address=values["address"],
        mediamtx_address=values["mediamtx_address"],
        recording_window_seconds=values["recording_window_seconds"],
        disk_watermark_percent=values["disk_watermark_percent"],
        status=values["status"],
        revision=values["revision"],
        created_by=values["created_by"],
        updated_by=values["updated_by"],
        created_at=values["created_at"],
        updated_at=values["updated_at"],
        credential_hash="",
    )


class PostgresInferenceHostRepository:
    """通过请求级会话访问 `device_inference_host`。"""

    def __init__(self, session: DatabaseSession) -> None:
        self._session = session
        self._has_credential_column: bool | None = None

    def add(self, host: InferenceHost) -> None:
        if self._supports_host_credentials():
            self._session.add(InferenceHostRow.from_domain(host))
        else:
            self._session.execute(
                insert(cast("Any", InferenceHostRow.__table__)).values(
                    **_host_values(host, include_credential=False)
                )
            )
        try:
            self._session.flush()
        except DatabaseError as error:
            _refuse_constraint_violation(error)

    def save(self, host: InferenceHost, *, expected_revision: int) -> None:
        values = _host_values(host, include_credential=self._supports_host_credentials())
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
                    select(InferenceHostRow.id).where(InferenceHostRow.id == host.id)
                ),
            )

    def by_id(self, host_id: UUID) -> InferenceHost | None:
        if self._supports_host_credentials():
            row = self._session.get(InferenceHostRow, host_id)
            return row.to_domain() if row is not None else None
        values = self._legacy_host_by_id(host_id)
        return _host_from_values(values) if values is not None else None

    def _supports_host_credentials(self) -> bool:
        if self._has_credential_column is None:
            self._has_credential_column = any(
                column["name"] == "credential_hash"
                for column in inspect(self._session.connection()).get_columns(
                    InferenceHostRow.__tablename__
                )
            )
        return self._has_credential_column

    def _legacy_host_by_id(self, host_id: UUID) -> Mapping[str, Any] | None:
        columns = [getattr(InferenceHostRow, name) for name in _HOST_BASE_COLUMNS]
        return cast(
            "Mapping[str, Any] | None",
            self._session.execute(select(*columns).where(InferenceHostRow.id == host_id))
            .mappings()
            .one_or_none(),
        )

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
            # 删除前由用例检查关联，这里保留外键拒绝作为数据库后备保护。
            _refuse_constraint_violation(
                error,
                foreign_key_to_host=DeviceRefusalCode.INFERENCE_HOST_HAS_BACKENDS,
                foreign_key_to_connector=DeviceRefusalCode.INFERENCE_HOST_HAS_CONNECTORS,
            )
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.scalar(
                    select(InferenceHostRow.id).where(InferenceHostRow.id == host_id)
                ),
            )
        return True

    def page_of(self, *, page: int, page_size: int) -> tuple[list[InferenceHost], int]:
        total = cast(
            "int", self._session.scalar(select(func.count()).select_from(InferenceHostRow))
        )
        if self._supports_host_credentials():
            rows = self._session.scalars(
                select(InferenceHostRow)
                .order_by(InferenceHostRow.created_at.desc(), InferenceHostRow.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            return [row.to_domain() for row in rows], total

        columns = [getattr(InferenceHostRow, name) for name in _HOST_BASE_COLUMNS]
        legacy_rows = (
            self._session.execute(
                select(*columns)
                .order_by(InferenceHostRow.created_at.desc(), InferenceHostRow.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            .mappings()
            .all()
        )
        return [_host_from_values(cast("Mapping[str, Any]", row)) for row in legacy_rows], total


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
                foreign_key_to_connector=DeviceRefusalCode.STATION_HAS_CONNECTORS,
                point_station_refusal=DeviceRefusalCode.STATION_HAS_POINTS,
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


class PostgresPointRepository:
    """通过请求事务保存 device_point。"""

    def __init__(self, session: DatabaseSession) -> None:
        self._session = session

    def add(self, point: Point) -> None:
        self._session.add(PointRow.from_domain(point))
        try:
            self._session.flush()
        except DatabaseError as error:
            _refuse_constraint_violation(
                error,
                point_station_refusal=DeviceRefusalCode.STATION_NOT_FOUND,
                point_connector_refusal=DeviceRefusalCode.CONNECTOR_NOT_FOUND,
            )

    def save(self, point: Point, *, expected_revision: int) -> None:
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    update(PointRow)
                    .where(PointRow.id == point.id, PointRow.revision == expected_revision)
                    .values(
                        station_id=point.station_id,
                        connector_id=point.connector_id,
                        direction=point.direction,
                        identifier=point.identifier,
                        semantic_label=point.semantic_label,
                        status=point.status,
                        revision=point.revision,
                        updated_by=point.updated_by,
                        updated_at=point.updated_at,
                    )
                ),
            )
        except DatabaseError as error:
            _refuse_constraint_violation(
                error,
                point_station_refusal=DeviceRefusalCode.STATION_NOT_FOUND,
                point_connector_refusal=DeviceRefusalCode.CONNECTOR_NOT_FOUND,
            )
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.POINT_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.scalar(
                    select(PointRow)
                    .where(PointRow.id == point.id)
                    .execution_options(populate_existing=True)
                ),
            )

    def by_id(self, point_id: UUID) -> Point | None:
        row = self._session.get(PointRow, point_id)
        return row.to_domain() if row is not None else None

    def remove(self, point_id: UUID, *, expected_revision: int) -> bool:
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                delete(PointRow).where(
                    PointRow.id == point_id,
                    PointRow.revision == expected_revision,
                )
            ),
        )
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.POINT_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.get(PointRow, point_id),
            )
        return True

    def any_for_station(self, station_id: UUID) -> bool:
        return bool(self._session.scalar(select(exists().where(PointRow.station_id == station_id))))

    def any_for_connector(self, connector_id: UUID) -> bool:
        return bool(
            self._session.scalar(select(exists().where(PointRow.connector_id == connector_id)))
        )

    def page_of(
        self,
        *,
        page: int,
        page_size: int,
        station_id: UUID | None,
        connector_id: UUID | None,
    ) -> tuple[list[Point], int]:
        count = select(func.count()).select_from(PointRow)
        listing = select(PointRow)
        if station_id is not None:
            count = count.where(PointRow.station_id == station_id)
            listing = listing.where(PointRow.station_id == station_id)
        if connector_id is not None:
            count = count.where(PointRow.connector_id == connector_id)
            listing = listing.where(PointRow.connector_id == connector_id)
        total = cast("int", self._session.scalar(count))
        rows = self._session.scalars(
            listing.order_by(PointRow.created_at.desc(), PointRow.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [row.to_domain() for row in rows], total


class PostgresConnectorRepository:
    """`device_connector` through the request's session."""

    def __init__(self, session: DatabaseSession) -> None:
        self._session = session

    def add(self, connector: Connector) -> None:
        self._session.add(ConnectorRow.from_domain(connector))
        try:
            self._session.flush()
        except DatabaseError as error:
            _refuse_constraint_violation(
                error,
                foreign_key_to_station=DeviceRefusalCode.STATION_NOT_FOUND,
                foreign_key_to_host=DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND,
            )

    def save(self, connector: Connector, *, expected_revision: int) -> None:
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    update(ConnectorRow)
                    .where(
                        ConnectorRow.id == connector.id,
                        ConnectorRow.revision == expected_revision,
                    )
                    .values(
                        station_id=connector.station_id,
                        host_id=connector.host_id,
                        name=connector.name,
                        connector_type=connector.connector_type,
                        configuration=connector.configuration.to_wire(),
                        credentials_configured=connector.credentials_configured,
                        reachability=connector.reachability,
                        health_detail=connector.health_detail,
                        capability=capability_to_wire(connector.capability),
                        status=connector.status,
                        revision=connector.revision,
                        updated_by=connector.updated_by,
                        updated_at=connector.updated_at,
                    )
                ),
            )
        except DatabaseError as error:
            _refuse_constraint_violation(
                error,
                foreign_key_to_station=DeviceRefusalCode.STATION_NOT_FOUND,
                foreign_key_to_host=DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND,
            )
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.CONNECTOR_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.get(ConnectorRow, connector.id),
            )

    def by_id(self, connector_id: UUID) -> Connector | None:
        row = self._session.get(ConnectorRow, connector_id)
        return row.to_domain() if row is not None else None

    def remove(self, connector_id: UUID, *, expected_revision: int) -> bool:
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    delete(ConnectorRow).where(
                        ConnectorRow.id == connector_id,
                        ConnectorRow.revision == expected_revision,
                    )
                ),
            )
        except DatabaseError as error:
            _refuse_constraint_violation(
                error,
                point_connector_refusal=DeviceRefusalCode.CONNECTOR_HAS_POINTS,
            )
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.CONNECTOR_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.get(ConnectorRow, connector_id),
            )
        return True

    def any_for_station(self, station_id: UUID) -> bool:
        return bool(
            self._session.scalar(select(exists().where(ConnectorRow.station_id == station_id)))
        )

    def any_for_host(self, host_id: UUID) -> bool:
        return bool(self._session.scalar(select(exists().where(ConnectorRow.host_id == host_id))))

    def for_station(self, station_id: UUID) -> list[Connector]:
        rows = self._session.scalars(
            select(ConnectorRow)
            .where(ConnectorRow.station_id == station_id)
            .order_by(ConnectorRow.created_at, ConnectorRow.id)
        ).all()
        return [row.to_domain() for row in rows]

    def page_of(
        self, *, page: int, page_size: int, station_id: UUID | None
    ) -> tuple[list[Connector], int]:
        query = select(func.count()).select_from(ConnectorRow)
        listing = select(ConnectorRow)
        if station_id is not None:
            query = query.where(ConnectorRow.station_id == station_id)
            listing = listing.where(ConnectorRow.station_id == station_id)
        total = cast("int", self._session.scalar(query))
        rows = self._session.scalars(
            listing.order_by(ConnectorRow.created_at.desc(), ConnectorRow.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [row.to_domain() for row in rows], total
