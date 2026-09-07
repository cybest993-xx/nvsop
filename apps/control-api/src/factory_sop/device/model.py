"""What `device` owns in this slice: the inference host and its process endpoints.

Pure domain types — no SQLAlchemy, no FastAPI, no clock, no network. Every instant arrives
from a caller holding one. The vocabulary is `CONTEXT.md`'s: the 推理机 is the physical
machine; the 推理后端 is a process endpoint carrying one template configuration — one host
runs several, and cameras will hang off the backend, not the machine (§5.10).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit
from uuid import UUID

from nvsop_contracts import Capability


class DeviceStatus(StrEnum):
    """Whether a configurable device still takes part in new bindings and operation.

    An enum rather than an `is_active` boolean: `CONTEXT.md` gives 停用 its own definition
    across every mutable configuration object. Deactivation is reversible and never
    cascades — a deactivated host's backends keep their rows and their history.
    """

    ACTIVE = "active"
    DEACTIVATED = "deactivated"


class ConnectionState(StrEnum):
    """What the last real connection test observed about a backend's endpoint.

    Three values, because Q31 fixes exactly these: a record that has never been tested is
    未验证 — not "working" — and only a test that truly reached the endpoint may say
    `success`. The facts belong to the placement (the host and endpoint a test observed):
    moving the backend, or pointing it at another endpoint, retires what was self-reported.
    """

    UNVERIFIED = "unverified"
    SUCCESS = "success"
    FAILURE = "failure"


def carries_userinfo(url: str) -> bool:
    """Report whether `url` is unsafe to persist as a device endpoint.

    A device endpoint may not carry userinfo, a query, or a fragment. Userinfo embeds a
    credential, while query and fragment components are common places for bearer tokens and
    other credentials to hide. The literal delimiters are rejected too, so an empty ``?`` or
    ``#`` cannot later become a secret-bearing URL through normalization. ADR-0008 and §5.12
    keep credentials off the center: not stored, not logged, and not returned by the API — so
    every URL this module persists passes this one check at its input contract.
    """
    parts = urlsplit(url)
    return parts.username is not None or parts.password is not None or "?" in url or "#" in url


@dataclass(frozen=True, slots=True)
class InferenceHost:
    """A physical inference machine: the autonomous judgment unit of its stations.

    The recording window and the disk watermark are per-host because the disk is the host's
    (§5.19), and the watermark is a safety threshold the edge runtime reports against.
    Health and clock offset are the host's to report; they arrive with the report contract.
    """

    id: UUID
    # Unique, and the natural key an operator matches on — never the URL identity (§5.15).
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

    def __post_init__(self) -> None:
        if self.recording_window_seconds <= 0:
            raise ValueError("recording_window_seconds must be greater than zero")
        if not 1 <= self.disk_watermark_percent <= 99:
            raise ValueError("disk_watermark_percent must be between 1 and 99")
        if self.mediamtx_address is not None and carries_userinfo(self.mediamtx_address):
            raise ValueError("device URLs cannot carry credentials, queries, or fragments")


@dataclass(frozen=True, slots=True)
class InferenceBackend:
    """One inference-service process endpoint carrying one template configuration.

    The connection fields record what the center observed by really asking the endpoint
    (§5.13). The center is not a model registry — no model table, no binding authority,
    nothing to distribute; `self_reported_model_ids` is an observed fact about a machine,
    kept for judgment provenance.
    """

    id: UUID
    # The one host this endpoint runs on. The foreign key is the database half of the
    # topology constraint; the use case is the other half, and the migration's trigger is
    # the third: a backend row may only be written while its host is active.
    host_id: UUID
    base_url: str
    # The one template configuration this endpoint carries — single-valued in the schema by
    # construction, which is the database half of "one backend, one template configuration"
    # (§5.10). Nullable until the `template` module lands (C5): its migration adds the foreign
    # key and the binding use case that writes it, and until then no writer exists, so the
    # column reads back `None` on every row.
    template_version_id: UUID | None
    status: DeviceStatus
    connection_state: ConnectionState
    connection_checked_at: datetime | None
    # Why the last test failed, for the operator's next step. Never a credential — the
    # endpoint URL carries none (ADR-0008: credentials live on the inference host).
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
