"""`device`'s tables: `device_inference_host` and `device_inference_backend` (§七).

The `device_` prefix is what makes migration ownership statically decidable, and
`scripts/check_migration_ownership.py` fails a migration that touches a table whose prefix
names a different module.

Mapped classes are separate from the domain types in `device/model.py` rather than being
them, for the same reason `auth`'s are: the domain types are frozen dataclasses with no
persistence machinery, which is what lets the use cases be tested without a database. The
translation is in this file, in one place per table.

A backend carries one nullable foreign key to `template_version`. The nullable value keeps
legacy and not-yet-bound backends valid; the device-owned follow-up migration adds the FK only
after the template module's immutable version table exists. A backend still has exactly one
slot, so there is no second device-side template configuration.
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
    Connector,
    ConnectorConfiguration,
    ConnectorReachability,
    ConnectorType,
    DeviceStatus,
    InferenceBackend,
    InferenceHost,
    MediaPathMode,
    PendingCommand,
    PendingCommandStatus,
    PendingCommandType,
    Point,
    PointDirection,
    RecordingMode,
    RuntimeParameterMode,
    Station,
    StationRuntimeParameters,
)
from factory_sop.persistence import Table
from nvsop_contracts import capability_from_wire, capability_to_wire


# 两张表共用一个 PostgreSQL 枚举类型；模块拥有的设备统一使用“停用”语义（CONTEXT.md）。数据库保存
# 枚举值而不是成员名，以保留 §5.15 规定的 snake_case 线格式。
def _status_enum(*, create_type: bool = True) -> Enum:
    return Enum(
        DeviceStatus,
        name="device_status",
        create_type=create_type,
        values_callable=lambda enum: [member.value for member in enum],
    )


def _runtime_mode_enum(*, create_type: bool = True) -> Enum:
    return Enum(
        RuntimeParameterMode,
        name="device_runtime_parameter_mode",
        create_type=create_type,
        values_callable=lambda enum: [member.value for member in enum],
    )


class InferenceHostRow(Table):
    """物理推理机持久化行。"""

    __tablename__ = "device_inference_host"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    # 唯一的自然键，供操作员匹配；不能代替 URL 身份（§5.15）。
    name: Mapped[str] = mapped_column(String(128), unique=True)
    address: Mapped[str] = mapped_column(String(255))
    # 预览配置完成前允许为空；后续相机绑定和预览切片才会依赖该地址，现在不能虚构默认值。
    mediamtx_address: Mapped[str | None] = mapped_column(String(255))
    # §5.19：滚动录制窗口按推理机配置并受磁盘约束；保存秒数，避免各客户端以不同字符串格式表达时长。
    recording_window_seconds: Mapped[int] = mapped_column(BigInteger())
    # §5.19：超过该水位表示机器即将无法录制，边缘运行时据此报告观测有效性中断；单位为磁盘百分比。
    disk_watermark_percent: Mapped[int] = mapped_column(Integer())
    status: Mapped[DeviceStatus] = mapped_column(_status_enum())
    # §5.15 的乐观锁版本；每次写入递增，预期版本不匹配时拒绝而不是覆盖。
    revision: Mapped[int] = mapped_column(Integer())
    # §5.15：归因使用这些列而不是审计表，保存操作账号的 UUID；不连接 `auth_user` 外键，避免账号管理
    # 被历史设备行阻塞。
    created_by: Mapped[UUID] = mapped_column(Uuid())
    updated_by: Mapped[UUID] = mapped_column(Uuid())
    # `timezone=True`：§5.15 固定 UTC 时刻；读回无时区值会破坏用例比较。
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    identity_public_key: Mapped[str | None] = mapped_column(String(4096))
    # 浏览器回放的独立原生接口地址；它与 WebRTC 信令地址都不携带凭据。
    mediamtx_playback_address: Mapped[str | None] = mapped_column(String(255))

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
            identity_public_key=self.identity_public_key,
            mediamtx_playback_address=self.mediamtx_playback_address,
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
            identity_public_key=host.identity_public_key,
            mediamtx_playback_address=host.mediamtx_playback_address,
        )


class InferenceHostIdentityNonceRow(Table):
    """主机签名请求的短期随机数，防止同一领取请求被重放。"""

    __tablename__ = "device_inference_host_identity_nonce"

    host_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("device_inference_host.id", ondelete="CASCADE"), primary_key=True
    )
    nonce: Mapped[str] = mapped_column(String(128), primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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
    runtime_parameter_mode: Mapped[RuntimeParameterMode] = mapped_column(_runtime_mode_enum())
    runtime_parameter_overrides: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(), nullable=True
    )
    runtime_parameters_revision: Mapped[int] = mapped_column(Integer())

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
            runtime_parameter_mode=self.runtime_parameter_mode,
            runtime_parameter_overrides=(
                None
                if self.runtime_parameter_overrides is None
                else StationRuntimeParameters.from_wire(self.runtime_parameter_overrides)
            ),
            runtime_parameters_revision=self.runtime_parameters_revision,
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
            runtime_parameter_mode=station.runtime_parameter_mode,
            runtime_parameter_overrides=(
                None
                if station.runtime_parameter_overrides is None
                else station.runtime_parameter_overrides.to_wire()
            ),
            runtime_parameters_revision=station.runtime_parameters_revision,
        )


class InferenceBackendRow(Table):
    """携带一套模板配置的推理服务进程端点持久化行。"""

    __tablename__ = "device_inference_backend"
    __table_args__ = (
        # 每个进程端点一行：同一推理机不能重复登记 URL，另一台机器的相同端口仍是另一端点（§5.10）。
        # 约束名称遵循元数据约定 `uq_device_inference_backend_host_id_base_url`。
        UniqueConstraint("host_id", "base_url"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    # `ondelete` 保持数据库默认的 NO ACTION：有后端的推理机不能删除，用例会先以同一规则拒绝。
    host_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("device_inference_host.id"), index=True
    )
    base_url: Mapped[str] = mapped_column(String(255))
    # 端点携带的一套模板配置使用单值列，因此模式天然保证“一后端一套配置”。历史未绑定后端为 NULL，
    # 非空值由 device-owned migration 外键约束到 immutable template_version。
    template_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(), ForeignKey("template_version.id"), nullable=True
    )
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
    # §5.13：端点自报的身份是机器观测事实，不是注册表条目；一条时间戳下保存一次探测的一组报告。
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
    media_path_mode: Mapped[MediaPathMode] = mapped_column(
        Enum(
            MediaPathMode,
            name="device_media_path_mode",
            values_callable=lambda enum: [member.value for member in enum],
        )
    )
    recording_mode: Mapped[RecordingMode] = mapped_column(
        Enum(
            RecordingMode,
            name="device_recording_mode",
            values_callable=lambda enum: [member.value for member in enum],
        )
    )

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
            media_path_mode=self.media_path_mode,
            recording_mode=self.recording_mode,
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
            media_path_mode=camera.media_path_mode,
            recording_mode=camera.recording_mode,
        )


class ConnectorRow(Table):
    """`device_connector`，中心只保存非秘密配置。"""

    __tablename__ = "device_connector"
    __table_args__ = (
        UniqueConstraint("station_id", "name", name="uq_device_connector_station_id_name"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    station_id: Mapped[UUID] = mapped_column(Uuid(), ForeignKey("device_station.id"), index=True)
    host_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("device_inference_host.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(128))
    connector_type: Mapped[ConnectorType] = mapped_column(
        Enum(
            ConnectorType,
            name="device_connector_type",
            values_callable=lambda enum: [member.value for member in enum],
        )
    )
    configuration: Mapped[dict[str, object]] = mapped_column(JSONB())
    credentials_configured: Mapped[bool] = mapped_column(Boolean())
    reachability: Mapped[ConnectorReachability] = mapped_column(
        Enum(
            ConnectorReachability,
            name="device_connector_reachability",
            values_callable=lambda enum: [member.value for member in enum],
        )
    )
    health_detail: Mapped[str | None] = mapped_column(String(255))
    capability: Mapped[dict[str, object]] = mapped_column(JSONB())
    status: Mapped[DeviceStatus] = mapped_column(_status_enum(create_type=False))
    revision: Mapped[int] = mapped_column(Integer())
    created_by: Mapped[UUID] = mapped_column(Uuid())
    updated_by: Mapped[UUID] = mapped_column(Uuid())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_domain(self) -> Connector:
        return Connector(
            id=self.id,
            station_id=self.station_id,
            host_id=self.host_id,
            name=self.name,
            connector_type=self.connector_type,
            configuration=ConnectorConfiguration.from_wire(self.configuration),
            credentials_configured=self.credentials_configured,
            reachability=self.reachability,
            health_detail=self.health_detail,
            capability=capability_from_wire(self.capability),
            status=self.status,
            revision=self.revision,
            created_by=self.created_by,
            updated_by=self.updated_by,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_domain(cls, connector: Connector) -> ConnectorRow:
        return cls(
            id=connector.id,
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
            created_by=connector.created_by,
            updated_by=connector.updated_by,
            created_at=connector.created_at,
            updated_at=connector.updated_at,
        )


class PointRow(Table):
    """一个授权输入或输出点位。"""

    __tablename__ = "device_point"
    __table_args__ = (
        UniqueConstraint(
            "station_id",
            "semantic_label",
            name="uq_device_point_station_id_semantic_label",
        ),
        UniqueConstraint(
            "connector_id",
            "direction",
            "identifier",
            name="uq_device_point_connector_id_direction_identifier",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    station_id: Mapped[UUID] = mapped_column(Uuid(), ForeignKey("device_station.id"), index=True)
    connector_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("device_connector.id"), index=True
    )
    direction: Mapped[PointDirection] = mapped_column(
        Enum(
            PointDirection,
            name="device_point_direction",
            values_callable=lambda enum: [member.value for member in enum],
        )
    )
    identifier: Mapped[str] = mapped_column(String(128))
    semantic_label: Mapped[str] = mapped_column(String(128))
    status: Mapped[DeviceStatus] = mapped_column(_status_enum(create_type=False))
    revision: Mapped[int] = mapped_column(Integer())
    created_by: Mapped[UUID] = mapped_column(Uuid())
    updated_by: Mapped[UUID] = mapped_column(Uuid())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_domain(self) -> Point:
        return Point(
            id=self.id,
            station_id=self.station_id,
            connector_id=self.connector_id,
            direction=self.direction,
            identifier=self.identifier,
            semantic_label=self.semantic_label,
            status=self.status,
            revision=self.revision,
            created_by=self.created_by,
            updated_by=self.updated_by,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_domain(cls, point: Point) -> PointRow:
        return cls(
            id=point.id,
            station_id=point.station_id,
            connector_id=point.connector_id,
            direction=point.direction,
            identifier=point.identifier,
            semantic_label=point.semantic_label,
            status=point.status,
            revision=point.revision,
            created_by=point.created_by,
            updated_by=point.updated_by,
            created_at=point.created_at,
            updated_at=point.updated_at,
        )


class PendingCommandRow(Table):
    """中心委托给推理机的命令，不保存设备凭据。"""

    __tablename__ = "device_pending_command"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_device_pending_command_idempotency_key"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    host_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("device_inference_host.id"), index=True
    )
    command_type: Mapped[PendingCommandType] = mapped_column(
        Enum(
            PendingCommandType,
            name="device_pending_command_type",
            values_callable=lambda enum: [member.value for member in enum],
        )
    )
    # 目标 ID 按命令类型解释；不加外键，为后续再切片命令保留同一队列。
    target_id: Mapped[UUID] = mapped_column(Uuid(), index=True)
    target_revision: Mapped[int] = mapped_column(Integer())
    idempotency_key: Mapped[str] = mapped_column(String(128))
    status: Mapped[PendingCommandStatus] = mapped_column(
        Enum(
            PendingCommandStatus,
            name="device_pending_command_status",
            values_callable=lambda enum: [member.value for member in enum],
        ),
        index=True,
    )
    attempt: Mapped[int] = mapped_column(Integer())
    claim_token: Mapped[str | None] = mapped_column(String(128))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[ConnectorReachability | None] = mapped_column(
        Enum(
            ConnectorReachability,
            name="device_connector_reachability",
            values_callable=lambda enum: [member.value for member in enum],
            create_type=False,
        )
    )
    result_detail: Mapped[str | None] = mapped_column(String(255))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    result_credentials_configured: Mapped[bool | None] = mapped_column(Boolean())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID] = mapped_column(Uuid())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_domain(self) -> PendingCommand:
        return PendingCommand(
            id=self.id,
            host_id=self.host_id,
            command_type=self.command_type,
            target_id=self.target_id,
            target_revision=self.target_revision,
            idempotency_key=self.idempotency_key,
            status=self.status,
            attempt=self.attempt,
            claim_token=self.claim_token,
            claimed_at=self.claimed_at,
            lease_expires_at=self.lease_expires_at,
            result=self.result,
            result_detail=self.result_detail,
            failure_code=self.failure_code,
            completed_at=self.completed_at,
            result_credentials_configured=self.result_credentials_configured,
            created_by=self.created_by,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_domain(cls, command: PendingCommand) -> PendingCommandRow:
        return cls(
            id=command.id,
            host_id=command.host_id,
            command_type=command.command_type,
            target_id=command.target_id,
            target_revision=command.target_revision,
            idempotency_key=command.idempotency_key,
            status=command.status,
            attempt=command.attempt,
            claim_token=command.claim_token,
            claimed_at=command.claimed_at,
            lease_expires_at=command.lease_expires_at,
            result=command.result,
            result_detail=command.result_detail,
            failure_code=command.failure_code,
            completed_at=command.completed_at,
            result_credentials_configured=command.result_credentials_configured,
            created_by=command.created_by,
            created_at=command.created_at,
            updated_at=command.updated_at,
        )
