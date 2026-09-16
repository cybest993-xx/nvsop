"""`device` 的小型跨模块契约。

模板模块只能通过本文件读取工位运行参数、校验正式绑定和调用主机身份认证；它不能
读取 `device` 的表或导入 `device.adapters`。具体实现仍由设备适配器在组合根接入。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from factory_sop.auth.api import Caller
from factory_sop.device.model import (
    InferenceHostIdentity,
    RuntimeParameterMode,
    Station,
    StationRuntimeConfiguration,
    StationRuntimeParameters,
)
from factory_sop.device.repository import (
    CameraRepository,
    ConnectorRepository,
    InferenceBackendRepository,
    InferenceHostRepository,
    PointRepository,
    StationRepository,
)
from nvsop_contracts import (
    Capability,
    HostIdentityRequest,
    PointRole,
    Unfitness,
    unfit_for,
)

INFERENCE_HOST_ID_HEADER = "X-Inference-Host-ID"
INFERENCE_HOST_TIMESTAMP_HEADER = "X-Inference-Host-Timestamp"
INFERENCE_HOST_NONCE_HEADER = "X-Inference-Host-Nonce"
INFERENCE_HOST_SIGNATURE_HEADER = "X-Inference-Host-Signature"


class BindingSignalKind(StrEnum):
    """正式绑定边界信号的种类。"""

    ACTION = "action"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class BindingSignal:
    """不依赖模板模块的边界信号值。"""

    kind: BindingSignalKind
    value: int | str


@dataclass(frozen=True, slots=True)
class TemplateBindingSpecification:
    """模板模块交给设备模块进行正式拓扑校验的最小数据。"""

    station_id: UUID
    template_version_id: UUID
    template_version_sha256: str
    start_signal: BindingSignal | None
    end_signals: tuple[BindingSignal, ...]
    runtime_mode: RuntimeParameterMode | None = None
    runtime_overrides: StationRuntimeParameters | None = None
    actor_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class BindingValidationIssue:
    """绑定校验的一项稳定机器原因和字段定位。"""

    code: str
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class TemplateBindingValidation:
    """正式绑定校验结果；预校验和提交前复核共用此形状。"""

    accepted: bool
    reasons: tuple[BindingValidationIssue, ...]
    station_revision: int | None = None
    runtime_parameters_revision: int | None = None
    backend_ids: tuple[UUID, ...] = ()


class DeviceTemplateBindingGateway(Protocol):
    """模板模块使用的设备拓扑和工位运行参数接缝。"""

    def validate_template_binding(
        self, specification: TemplateBindingSpecification
    ) -> TemplateBindingValidation:
        """无写入地重新读取设备拓扑并校验模板边界、能力和运行参数。"""
        ...

    def apply_template_binding(
        self,
        specification: TemplateBindingSpecification,
        *,
        expected_station_revision: int,
        now: datetime,
    ) -> TemplateBindingValidation:
        """在同一请求事务内原子更新工位运行参数和参与后端的模板槽位。"""
        ...

    def read_station_runtime_parameters(
        self,
        station_id: UUID,
        *,
        defaults: StationRuntimeParameters | None,
    ) -> StationRuntimeConfiguration:
        """返回不逐项合并的模式、默认值、覆盖组和生效组。"""
        ...

    def update_station_runtime_parameters(
        self,
        station_id: UUID,
        *,
        mode: RuntimeParameterMode,
        overrides: StationRuntimeParameters | None,
        expected_station_revision: int,
        actor_id: UUID,
        now: datetime,
        defaults: StationRuntimeParameters | None,
    ) -> StationRuntimeConfiguration:
        """以工位版本条件更新完整运行参数组，并推进配置修订。"""
        ...

    def participating_backend_ids(self, station_id: UUID) -> tuple[UUID, ...]:
        """返回当前工位活动相机参与的去重后端集合。"""
        ...

    def owns_station_backend(self, *, host_id: UUID, station_id: UUID, backend_id: UUID) -> bool:
        """判断认证主机是否拥有该工位上的该推理后端。"""
        ...


class DeviceHostGateway(Protocol):
    """模板、监控上报入口调用的主机身份接缝。"""

    def authenticate(self, *, host: InferenceHostIdentity, now: datetime) -> None:
        """复用登记公钥、签名和 nonce 的正式主机认证。"""
        ...

    def owns_station(self, *, host_id: UUID, station_id: UUID) -> bool:
        """只允许认证主机上报自己拥有的工位健康。"""
        ...

    def owns_station_backend(self, *, host_id: UUID, station_id: UUID, backend_id: UUID) -> bool:
        """只允许认证主机上报自己拥有的工位和推理后端。"""
        ...


class DeviceHistoricalAssignmentGateway(Protocol):
    """供监控上报入口验证 Center 已下发历史配置归属的设备接缝。"""

    def has_configuration_assignment(
        self,
        *,
        host_id: UUID,
        configuration_revision: int,
        configuration_sha256: str,
        station_id: UUID,
        backend_id: UUID,
        template_version_id: str | None,
        template_sha256: str | None,
        model_ids: tuple[str, ...],
    ) -> bool:
        """仅在完整上下文精确匹配不可变历史 assignment 时返回真。"""
        ...


class StationCodeLookup(Protocol):
    """设备模块为自然编码查询提供的最小存储 seam。"""

    def by_code(self, code: str) -> Station | None:
        """返回编码匹配的工位。"""
        ...


@dataclass(frozen=True, slots=True)
class StationReference:
    """供其他模块做自然编码匹配的最小工位身份。"""

    id: UUID
    code: str
    name: str


def station_by_code(*, code: str, stations: StationCodeLookup) -> StationReference | None:
    """按工位编码返回最小身份；模板导入用它验证工作簿引用。"""
    station: Station | None = stations.by_code(code)
    if station is None:
        return None
    return StationReference(id=station.id, code=station.code, name=station.name)


def capability_unfitness(
    capability: Capability, *, role: PointRole, budget_seconds: float
) -> tuple[Unfitness, ...]:
    """调用共享 `unfit_for` 规则，保证预校验与既有点位绑定一致。"""
    return unfit_for(capability, role=role, budget=budget_seconds)


def host_identity_from_headers(
    *,
    host_id: UUID,
    method: str,
    path: str,
    body: Mapping[str, object] | None,
    timestamp: str | None,
    nonce: str | None,
    signature: str | None,
) -> InferenceHostIdentity:
    """解析真实主机请求头；认证身份只来自 `X-Inference-Host-ID`。"""
    request: HostIdentityRequest | None = None
    if timestamp is not None and nonce is not None:
        try:
            request = HostIdentityRequest(
                method=method,
                path=path,
                host_id=str(host_id),
                timestamp=int(timestamp),
                nonce=nonce,
                body=body,
            )
        except (TypeError, ValueError):
            request = None
    return InferenceHostIdentity(host_id=host_id, request=request, signature=signature)


def summary(
    *,
    caller: Caller,
    hosts: InferenceHostRepository,
    backends: InferenceBackendRepository,
    stations: StationRepository,
    cameras: CameraRepository,
    connectors: ConnectorRepository,
    points: PointRepository,
) -> dict[str, object]:
    """返回 overview 使用的权限裁剪设备摘要。"""
    from factory_sop.device.usecases.summary import summary as build_summary

    return build_summary(
        caller=caller,
        hosts=hosts,
        backends=backends,
        stations=stations,
        cameras=cameras,
        connectors=connectors,
        points=points,
    )


def authenticate_host(
    *, host: InferenceHostIdentity, now: datetime, hosts: InferenceHostRepository
) -> None:
    """通过 `device.api` 转发到现有认证用例，不暴露适配器。"""
    # 运行时导入避免 `api.py` 与用例层形成模块初始化环；调用仍然只经过设备模块内部。
    from factory_sop.device.usecases.commands import authenticate_command_host

    authenticate_command_host(host=host, now=now, hosts=hosts)


__all__ = [
    "INFERENCE_HOST_ID_HEADER",
    "INFERENCE_HOST_NONCE_HEADER",
    "INFERENCE_HOST_SIGNATURE_HEADER",
    "INFERENCE_HOST_TIMESTAMP_HEADER",
    "BindingSignal",
    "BindingSignalKind",
    "BindingValidationIssue",
    "DeviceHistoricalAssignmentGateway",
    "DeviceHostGateway",
    "DeviceTemplateBindingGateway",
    "StationCodeLookup",
    "StationReference",
    "TemplateBindingSpecification",
    "TemplateBindingValidation",
    "authenticate_host",
    "capability_unfitness",
    "host_identity_from_headers",
    "station_by_code",
    "summary",
]
