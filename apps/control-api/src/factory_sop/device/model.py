"""device 本阶段拥有的推理机和进程端点领域模型。

这里只放纯领域类型，不依赖 SQLAlchemy、FastAPI、时钟或网络。所有时刻都由持有时刻的调用方传入。
推理机是物理设备，推理后端是携带一套模板配置的进程端点；一台推理机可运行多个后端，相机绑定后端而不是
直接绑定推理机（§5.10）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit
from uuid import UUID

from nvsop_contracts import (
    Capability,
    HostIdentityRequest,
    validate_host_identity_public_key,
)


class DeviceStatus(StrEnum):
    """可配置设备是否继续参与新绑定和运行。

    使用枚举而不是 `is_active` 布尔值，因为所有可变配置对象共用“停用”语义。停用可恢复且不级联，
    已停用推理机的后端仍保留记录和历史。
    """

    ACTIVE = "active"
    DEACTIVATED = "deactivated"


@dataclass(frozen=True, slots=True)
class InferenceHostIdentity:
    """请求呈现的主机标识和一次公钥签名证明。"""

    host_id: UUID
    request: HostIdentityRequest | None = None
    signature: str | None = None


class ConnectionState(StrEnum):
    """最近一次真实连接测试对后端端点的观测结果。

    固定为三态：从未测试是“未验证”，不是“正常”；只有真实到达端点的测试才能报告成功。事实属于
    测试时的部署位置；移动后端或改变端点后，原有自报结果失效。
    """

    UNVERIFIED = "unverified"
    SUCCESS = "success"
    FAILURE = "failure"


def carries_userinfo(url: str) -> bool:
    """判断设备端点 URL 是否不应持久化。

    端点不得携带 userinfo、查询串或片段。userinfo 可能嵌入口令，查询串和片段也常被用来隐藏 bearer
    token 等凭据；即使分隔符为空也拒绝，避免规范化后变成秘密。ADR-0008 和 §5.12 要求中心不存储、
    不记录、不返回凭据，因此本模块所有持久化 URL 都在输入处经过这一检查。
    """
    parts = urlsplit(url)
    return parts.username is not None or parts.password is not None or "?" in url or "#" in url


@dataclass(frozen=True, slots=True)
class InferenceHost:
    """物理推理机，也是其工位的自治判定单元。

    录制窗口和磁盘水位按推理机配置，因为磁盘属于推理机（§5.19），边缘运行时据此报告安全阈值。
    健康状态和时钟偏移由推理机报告，并通过报告契约进入中心。
    """

    id: UUID
    # 唯一的自然键，供操作员匹配；不能代替 URL 身份（§5.15）。
    name: str
    address: str
    mediamtx_address: str | None
    recording_window_seconds: int
    disk_watermark_percent: int
    status: DeviceStatus
    revision: int
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime
    # 中心仅保存非秘密公钥；空值表示尚未配置主机控制面身份。
    identity_public_key: str | None = None

    def __post_init__(self) -> None:
        if self.identity_public_key:
            validate_host_identity_public_key(self.identity_public_key)
        if self.recording_window_seconds <= 0:
            raise ValueError("recording_window_seconds must be greater than zero")
        if not 1 <= self.disk_watermark_percent <= 99:
            raise ValueError("disk_watermark_percent must be between 1 and 99")
        if self.mediamtx_address is not None and carries_userinfo(self.mediamtx_address):
            raise ValueError("device URLs cannot carry credentials, queries, or fragments")


@dataclass(frozen=True, slots=True)
class InferenceBackend:
    """携带一套模板配置的推理服务进程端点。

    连接字段记录中心真实访问端点得到的观测（§5.13）。中心不是模型注册表，不保存模型表、不负责绑定、
    也不分发模型；`self_reported_model_ids` 只是推理机自报的观测事实，用于判定溯源。
    """

    id: UUID
    # 端点运行所在的推理机。外键、用例校验和迁移触发器共同保证拓扑约束，且仅允许写入活动推理机。
    host_id: UUID
    base_url: str
    # 端点携带的一套模板配置。字段保持单值，数据库因此保证“一个后端、一套模板配置”（§5.10）。
    # template 模块落地前允许为空（C5）；后续迁移和绑定用例会加入外键和写入路径。
    template_version_id: UUID | None
    status: DeviceStatus
    connection_state: ConnectionState
    connection_checked_at: datetime | None
    # 最近一次测试失败的原因，供操作员处理；不是凭据，端点 URL 也不携带凭据（ADR-0008）。
    connection_detail: str | None
    self_reported_model_ids: tuple[str, ...]
    self_reported_at: datetime | None
    revision: int
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if carries_userinfo(self.base_url):
            raise ValueError("device URLs cannot carry credentials, queries, or fragments")


@dataclass(frozen=True, slots=True)
class Station:
    """最小的独立 SOP 工作空间，以唯一的操作编码标识。"""

    id: UUID
    code: str
    name: str
    tags: tuple[str, ...]
    status: DeviceStatus
    revision: int
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("station code must not be empty")
        if not self.name:
            raise ValueError("station name must not be empty")
        if any(not tag for tag in self.tags):
            raise ValueError("station tags must not be empty")


@dataclass(frozen=True, slots=True)
class Camera:
    """分配给一个工位及一套推理机/后端拓扑的相机视频流对。"""

    id: UUID
    name: str
    address: str
    main_stream_path: str
    sub_stream_path: str
    credentials_configured: bool
    station_id: UUID
    host_id: UUID
    backend_id: UUID
    status: DeviceStatus
    revision: int
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("camera name must not be empty")
        if not self.address:
            raise ValueError("camera address must not be empty")
        if not self.main_stream_path:
            raise ValueError("main_stream_path must not be empty")
        if not self.sub_stream_path:
            raise ValueError("sub_stream_path must not be empty")
        for value in (self.address, self.main_stream_path, self.sub_stream_path):
            if carries_userinfo(value):
                raise ValueError("camera URLs cannot carry credentials, queries, or fragments")


class ConnectorType(StrEnum):
    """中心支持的连接器配置类型。"""

    HIKVISION_ISAPI = "hikvision_isapi"
    BOARD_CARD = "board_card"


class ConnectorReachability(StrEnum):
    """连接器尚未由推理机真实验证时的显式状态。"""

    UNVERIFIED = "unverified"
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"


def contains_connector_credential(value: str) -> bool:
    """拒绝地址中的凭据、查询串和片段，避免秘密进入中心。"""
    parts = urlsplit(value)
    return (
        parts.username is not None
        or parts.password is not None
        or "?" in value
        or "#" in value
        or "@" in value
    )


@dataclass(frozen=True, slots=True)
class ConnectorConfiguration:
    """连接器允许保存的非秘密参数；未知字段不属于本阶段契约。"""

    address: str
    port: int | None = None

    def __post_init__(self) -> None:
        if not self.address or len(self.address) > 255:
            raise ValueError("connector address must be between 1 and 255 characters")
        if contains_connector_credential(self.address):
            raise ValueError("connector configuration cannot contain credentials")
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError("connector port must be between 1 and 65535")

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> ConnectorConfiguration:
        """从严格的 JSON 对象构造配置，不接受额外键或非标量值。"""
        allowed = {"address", "port"}
        if set(value) - allowed or "address" not in value:
            raise ValueError("connector configuration fields are not supported")
        address = value["address"]
        port = value.get("port")
        if not isinstance(address, str) or (
            port is not None and (not isinstance(port, int) or isinstance(port, bool))
        ):
            raise ValueError("connector configuration has an invalid field type")
        return cls(address=address, port=port)

    def to_wire(self) -> dict[str, str | int]:
        """返回可安全写入 JSONB 的配置对象。"""
        result: dict[str, str | int] = {"address": self.address}
        if self.port is not None:
            result["port"] = self.port
        return result


@dataclass(frozen=True, slots=True)
class Connector:
    """绑定到工位和推理机的中心配置，不保存设备凭据。"""

    id: UUID
    station_id: UUID
    host_id: UUID
    name: str
    connector_type: ConnectorType
    configuration: ConnectorConfiguration
    credentials_configured: bool
    reachability: ConnectorReachability
    health_detail: str | None
    capability: Capability
    status: DeviceStatus
    revision: int
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("connector name must not be empty")


class PointDirection(StrEnum):
    """授权点位的物理读写方向。"""

    INPUT = "input"
    OUTPUT = "output"


@dataclass(frozen=True, slots=True)
class Point:
    """由工位和连接器共同定位、以语义标签供绑定引用的点位。"""

    id: UUID
    station_id: UUID
    connector_id: UUID
    direction: PointDirection
    identifier: str
    semantic_label: str
    status: DeviceStatus
    revision: int
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.identifier or len(self.identifier) > 128:
            raise ValueError("point identifier must be between 1 and 128 characters")
        if not self.semantic_label or len(self.semantic_label) > 128:
            raise ValueError("point semantic label must be between 1 and 128 characters")


class BindingReasonCode(StrEnum):
    """绑定前校验返回的稳定原因码。"""

    POINT_REQUIRED = "point_required"
    POINT_NOT_FOUND = "point_not_found"
    POINT_STATION_MISMATCH = "point_station_mismatch"
    POINT_DEACTIVATED = "point_deactivated"
    CONNECTOR_NOT_FOUND = "connector_not_found"
    CONNECTOR_DEACTIVATED = "connector_deactivated"
    WRONG_DIRECTION = "wrong_direction"
    CAPABILITY_UNVERIFIED = "capability_unverified"
    MAY_DROP_EDGES = "may_drop_edges"
    NOT_SEQUENCED = "not_sequenced"
    DELIVERY_TOO_SLOW = "delivery_too_slow"


@dataclass(frozen=True, slots=True)
class BindingReason:
    """可直接展示给操作员的一项绑定拒绝。"""

    code: BindingReasonCode
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class BindingValidation:
    """绑定前校验的完整结果；校验本身不创建模板绑定。"""

    accepted: bool
    reasons: tuple[BindingReason, ...]


class PendingCommandType(StrEnum):
    """推理机从中心领取的设备命令类型。"""

    TEST_CONNECTOR_CONNECTION = "test_connector_connection"


class PendingCommandStatus(StrEnum):
    """持久委托命令的生命周期状态。"""

    PENDING = "pending"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class PendingCommand:
    """一条不含凭据的委托命令及其领取租约和真实结果。"""

    id: UUID
    host_id: UUID
    command_type: PendingCommandType
    target_id: UUID
    target_revision: int
    idempotency_key: str
    status: PendingCommandStatus
    attempt: int
    claim_token: str | None
    claimed_at: datetime | None
    lease_expires_at: datetime | None
    result: ConnectorReachability | None
    result_detail: str | None
    failure_code: str | None
    completed_at: datetime | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    result_credentials_configured: bool | None = None

    def __post_init__(self) -> None:
        if not self.idempotency_key or len(self.idempotency_key) > 128:
            raise ValueError("command idempotency_key must be between 1 and 128 characters")
        if self.target_revision < 1:
            raise ValueError("command target_revision must be positive")
        if self.attempt < 0:
            raise ValueError("command attempt must not be negative")


@dataclass(frozen=True, slots=True)
class PendingCommandCompletion:
    """一次命令完成写入的完整上下文，避免调用方拆散同一组字段。"""

    command_id: UUID
    host_id: UUID
    claim_token: str
    status: PendingCommandStatus
    result: ConnectorReachability | None
    result_detail: str | None
    failure_code: str | None
    completed_at: datetime
    result_credentials_configured: bool | None = None

    def __post_init__(self) -> None:
        if self.result is not None and self.result_credentials_configured is not True:
            raise ValueError("a non-rejected completion must confirm configured credentials")


@dataclass(frozen=True, slots=True)
class ConnectorTestResult:
    """推理机真实连接测试的可持久化结果，不携带设备凭据。"""

    reachability: ConnectorReachability | None
    detail: str | None = None
    credentials_configured: bool | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if self.reachability is None and not self.failure_code:
            raise ValueError("a rejected connection test must carry a failure code")
        if self.reachability is not None and self.credentials_configured is not True:
            raise ValueError("a non-rejected connection test must confirm configured credentials")
