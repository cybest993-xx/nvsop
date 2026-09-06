"""`device`'s tables: `device_inference_host` and `device_inference_backend` (§七).

The `device_` prefix is what makes migration ownership statically decidable, and
`scripts/check_migration_ownership.py` fails a migration that touches a table whose prefix
names a different module.

Mapped classes are separate from the domain types in `device/model.py` rather than being
them, for the same reason `auth`'s are: the domain types are frozen dataclasses with no
persistence machinery, which is what lets the use cases be tested without a database. The
translation is in this file, in one place per table.

There is no column for the template a backend carries yet: the `template` module and its
tables do not exist (C5), and a nullable foreign key to a table nobody can create would be
schema fiction. The binding arrives with template's own migration as a single-valued
column — the shape that makes "one backend, one template configuration" impossible to
violate at the database, which is what §5.10 asks for.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from factory_sop.device.model import (
    Camera,
    ConnectionState,
    DeviceStatus,
    InferenceBackend,
    InferenceHost,
    Station,
)
from factory_sop.persistence import Table


# One PostgreSQL enum type, carried by both tables: 停用 means the same thing for every
# device the module owns (CONTEXT.md). The values are the enum's members, not its names,
# so the wire spelling in §5.15's snake_case survives in the database.
def _status_enum(*, create_type: bool = True) -> Enum:
    return Enum(
        DeviceStatus,
        name="device_status",
        create_type=create_type,
        values_callable=lambda enum: [member.value for member in enum],
    )


class InferenceHostRow(Table):
    """A physical inference machine."""

    __tablename__ = "device_inference_host"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    # Unique, and the natural key an operator matches on — never the URL identity (§5.15).
    name: Mapped[str] = mapped_column(String(128), unique=True)
    address: Mapped[str] = mapped_column(String(255))
    # Optional until preview is configured: the address becomes load-bearing with the
    # camera-binding and preview slices, and inventing a value for it now would be fiction.
    mediamtx_address: Mapped[str | None] = mapped_column(String(255))
    # §5.19: the rolling recording window is per host, bounded by its disk, and a
    # configurable duration — carried in seconds so a duration is a number, not a string
    # each client formats differently.
    recording_window_seconds: Mapped[int] = mapped_column(BigInteger())
    # §5.19: past this watermark the machine is about to stop being able to record, which
    # the edge runtime reports as an observation-validity break. A percentage of the disk.
    disk_watermark_percent: Mapped[int] = mapped_column(Integer())
    status: Mapped[DeviceStatus] = mapped_column(_status_enum())
    # §5.15's optimistic-locking revision: every write moves it, and a save whose expected
    # revision no longer matches refuses instead of overwriting.
    revision: Mapped[int] = mapped_column(Integer())
    # §5.15: attribution is these columns, not an audit table. Plain UUIDs of the acting
    # account — deliberately no foreign key to `auth_user`, so account administration can
    # never be blocked by a device row that merely remembers who touched it.
    created_by: Mapped[UUID] = mapped_column(Uuid())
    updated_by: Mapped[UUID] = mapped_column(Uuid())
    # `timezone=True`: §5.15 fixes UTC timestamps, and a naive read-back would break every
    # comparison the use cases make.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_domain(self) -> InferenceHost:
        return InferenceHost(
            id=self.id,
            name=self.name,
            address=self.address,
            mediamtx_address=self.mediamtx_address,
            recording_window_seconds=self.recording_window_seconds,
            disk_watermark_percent=self.disk_watermark_percent,
            status=self.status,
            revision=self.revision,
            created_by=self.created_by,
            updated_by=self.updated_by,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_domain(cls, host: InferenceHost) -> InferenceHostRow:
        return cls(
            id=host.id,
            name=host.name,
            address=host.address,
            mediamtx_address=host.mediamtx_address,
            recording_window_seconds=host.recording_window_seconds,
            disk_watermark_percent=host.disk_watermark_percent,
            status=host.status,
            revision=host.revision,
            created_by=host.created_by,
            updated_by=host.updated_by,
            created_at=host.created_at,
            updated_at=host.updated_at,
        )


class StationRow(Table):
    __tablename__ = "device_station"
    __table_args__ = (UniqueConstraint("code"),)

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    tags: Mapped[list[str]] = mapped_column(JSONB())
    status: Mapped[DeviceStatus] = mapped_column(_status_enum(create_type=False))
    revision: Mapped[int] = mapped_column(Integer())
    created_by: Mapped[UUID] = mapped_column(Uuid())
    updated_by: Mapped[UUID] = mapped_column(Uuid())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_domain(self) -> Station:
        return Station(
            id=self.id,
            code=self.code,
            name=self.name,
            tags=tuple(self.tags),
            status=self.status,
            revision=self.revision,
            created_by=self.created_by,
            updated_by=self.updated_by,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_domain(cls, station: Station) -> StationRow:
        return cls(
            id=station.id,
            code=station.code,
            name=station.name,
            tags=list(station.tags),
            status=station.status,
            revision=station.revision,
            created_by=station.created_by,
            updated_by=station.updated_by,
            created_at=station.created_at,
            updated_at=station.updated_at,
        )


class InferenceBackendRow(Table):
    """One inference-service process endpoint carrying one template configuration."""

    __tablename__ = "device_inference_backend"
    __table_args__ = (
        # One row per process endpoint: the same host cannot register one URL twice, while
        # the same port on another machine is another endpoint (§5.10's topology). Named by
        # the metadata convention as `uq_device_inference_backend_host_id_base_url`.
        UniqueConstraint("host_id", "base_url"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    # `ondelete` stays at the database's NO ACTION: a host with backends cannot be deleted,
    # which is the database half of the topology constraint the use case refuses first.
    host_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("device_inference_host.id"), index=True
    )
    base_url: Mapped[str] = mapped_column(String(255))
    # The one template configuration this endpoint carries — a scalar column, so "one
    # backend, one template configuration" holds in the schema by construction. No foreign
    # key yet: `template_version` is C5's table, and its migration adds the key.
    template_version_id: Mapped[UUID | None] = mapped_column(Uuid())
    status: Mapped[DeviceStatus] = mapped_column(_status_enum(create_type=False))
    connection_state: Mapped[ConnectionState] = mapped_column(
        Enum(
            ConnectionState,
            name="device_connection_state",
            values_callable=lambda enum: [member.value for member in enum],
        )
    )
    connection_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connection_detail: Mapped[str | None] = mapped_column(String(255))
    # §5.13: the identities the endpoint self-reported, an observed fact about the machine —
    # not a registry entry. A list under one timestamp: one probe, one report.
    self_reported_model_ids: Mapped[list[str] | None] = mapped_column(JSONB())
    self_reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer())
    created_by: Mapped[UUID] = mapped_column(Uuid())
    updated_by: Mapped[UUID] = mapped_column(Uuid())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_domain(self) -> InferenceBackend:
        return InferenceBackend(
            id=self.id,
            host_id=self.host_id,
            base_url=self.base_url,
            template_version_id=self.template_version_id,
            status=self.status,
            connection_state=self.connection_state,
            connection_checked_at=self.connection_checked_at,
            connection_detail=self.connection_detail,
            self_reported_model_ids=tuple(self.self_reported_model_ids or ()),
            self_reported_at=self.self_reported_at,
            revision=self.revision,
            created_by=self.created_by,
            updated_by=self.updated_by,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_domain(cls, backend: InferenceBackend) -> InferenceBackendRow:
        return cls(
            id=backend.id,
            host_id=backend.host_id,
            base_url=backend.base_url,
            template_version_id=backend.template_version_id,
            status=backend.status,
            connection_state=backend.connection_state,
            connection_checked_at=backend.connection_checked_at,
            connection_detail=backend.connection_detail,
            self_reported_model_ids=list(backend.self_reported_model_ids),
            self_reported_at=backend.self_reported_at,
            revision=backend.revision,
            created_by=backend.created_by,
            updated_by=backend.updated_by,
            created_at=backend.created_at,
            updated_at=backend.updated_at,
        )


class CameraRow(Table):
    __tablename__ = "device_camera"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    address: Mapped[str] = mapped_column(String(255))
    main_stream_path: Mapped[str] = mapped_column(String(255))
    sub_stream_path: Mapped[str] = mapped_column(String(255))
    credentials_configured: Mapped[bool] = mapped_column(Boolean(), default=False)
    station_id: Mapped[UUID] = mapped_column(Uuid(), ForeignKey("device_station.id"), index=True)
    host_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("device_inference_host.id"), index=True
    )
    backend_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("device_inference_backend.id"), index=True
    )
    status: Mapped[DeviceStatus] = mapped_column(_status_enum(create_type=False))
    revision: Mapped[int] = mapped_column(Integer())
    created_by: Mapped[UUID] = mapped_column(Uuid())
    updated_by: Mapped[UUID] = mapped_column(Uuid())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_domain(self) -> Camera:
        return Camera(
            id=self.id,
            name=self.name,
            address=self.address,
            main_stream_path=self.main_stream_path,
            sub_stream_path=self.sub_stream_path,
            credentials_configured=self.credentials_configured,
            station_id=self.station_id,
            host_id=self.host_id,
            backend_id=self.backend_id,
            status=self.status,
            revision=self.revision,
            created_by=self.created_by,
            updated_by=self.updated_by,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_domain(cls, camera: Camera) -> CameraRow:
        return cls(
            id=camera.id,
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
            created_by=camera.created_by,
            updated_by=camera.updated_by,
            created_at=camera.created_at,
            updated_at=camera.updated_at,
        )
