"""配置组装读取的最小结构化接缝。

配置模块只需要设备拓扑和模板发布快照的少量事实。这里用 Protocol 描述这些事实，
避免组装用例直接穿透到其他模块的领域模型或仓储实现；HTTP 适配器在组合根把真实仓储
传入同一个接缝。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from nvsop_contracts import Capability


class EnumSnapshot(Protocol):
    """能提供稳定字符串值的领域枚举。"""

    @property
    def value(self) -> str: ...


class RuntimeParametersSnapshot(Protocol):
    """一组已经完成校验的工位运行参数。"""

    @property
    def idle_timeout_seconds(self) -> float: ...

    @property
    def step_deadline_seconds(self) -> float: ...

    @property
    def disposition_policy(self) -> str: ...


class HostSnapshot(Protocol):
    """配置组装所需的推理机身份和修订号。"""

    @property
    def id(self) -> UUID: ...

    @property
    def revision(self) -> int: ...


class BackendSnapshot(Protocol):
    """配置组装所需的后端归属和修订号。"""

    @property
    def id(self) -> UUID: ...

    @property
    def host_id(self) -> UUID: ...

    @property
    def status(self) -> EnumSnapshot: ...

    @property
    def revision(self) -> int: ...

    @property
    def self_reported_model_ids(self) -> tuple[str, ...]: ...


class CameraSnapshot(Protocol):
    """配置组装所需的相机拓扑事实。"""

    @property
    def id(self) -> UUID: ...

    @property
    def name(self) -> str: ...

    @property
    def address(self) -> str: ...

    @property
    def main_stream_path(self) -> str: ...

    @property
    def sub_stream_path(self) -> str: ...

    @property
    def credentials_configured(self) -> bool: ...

    @property
    def station_id(self) -> UUID: ...

    @property
    def host_id(self) -> UUID: ...

    @property
    def backend_id(self) -> UUID: ...

    @property
    def status(self) -> EnumSnapshot: ...

    @property
    def revision(self) -> int: ...

    @property
    def media_path_mode(self) -> EnumSnapshot: ...

    @property
    def recording_mode(self) -> EnumSnapshot: ...


class StationSnapshot(Protocol):
    """配置组装所需的工位身份、运行参数和修订号。"""

    @property
    def id(self) -> UUID: ...

    @property
    def code(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def status(self) -> EnumSnapshot: ...

    @property
    def revision(self) -> int: ...

    @property
    def runtime_parameters_revision(self) -> int: ...

    @property
    def runtime_parameter_mode(self) -> EnumSnapshot: ...

    @property
    def runtime_parameter_overrides(self) -> RuntimeParametersSnapshot | None: ...


class ConnectorConfigurationSnapshot(Protocol):
    """连接器非秘密地址配置。"""

    @property
    def address(self) -> str: ...

    @property
    def port(self) -> int | None: ...


class ConnectorSnapshot(Protocol):
    """配置组装所需的连接器事实。"""

    @property
    def id(self) -> UUID: ...

    @property
    def station_id(self) -> UUID: ...

    @property
    def host_id(self) -> UUID: ...

    @property
    def name(self) -> str: ...

    @property
    def connector_type(self) -> EnumSnapshot: ...

    @property
    def configuration(self) -> ConnectorConfigurationSnapshot: ...

    @property
    def revision(self) -> int: ...

    @property
    def capability(self) -> Capability: ...

    @property
    def status(self) -> EnumSnapshot: ...


class PointSnapshot(Protocol):
    """配置组装所需的点位事实。"""

    @property
    def id(self) -> UUID: ...

    @property
    def station_id(self) -> UUID: ...

    @property
    def connector_id(self) -> UUID: ...

    @property
    def direction(self) -> EnumSnapshot: ...

    @property
    def semantic_label(self) -> str: ...

    @property
    def identifier(self) -> str: ...

    @property
    def status(self) -> EnumSnapshot: ...

    @property
    def revision(self) -> int: ...


class TemplateBindingSnapshot(Protocol):
    """工位期望绑定的不可变引用。"""

    @property
    def desired_version_id(self) -> UUID: ...

    @property
    def desired_sha256(self) -> str: ...

    @property
    def desired_config_revision(self) -> int: ...


class RuntimeDefaultsSnapshot(Protocol):
    """模板发布版本携带的运行参数默认值。"""

    @property
    def idle_timeout_seconds(self) -> float | None: ...

    @property
    def step_deadline_seconds(self) -> float | None: ...

    @property
    def disposition_policy(self) -> str | None: ...


class ArtifactSnapshot(Protocol):
    """模板版本中的一个已校验制品。"""

    @property
    def name(self) -> EnumSnapshot: ...

    @property
    def media_type(self) -> str: ...

    @property
    def content(self) -> bytes: ...

    @property
    def sha256(self) -> str: ...


class TemplateVersionSnapshot(Protocol):
    """配置组装所需的已发布模板版本。"""

    @property
    def id(self) -> UUID: ...

    @property
    def template_id(self) -> UUID: ...

    @property
    def source_draft_revision(self) -> int: ...

    @property
    def runtime_defaults(self) -> RuntimeDefaultsSnapshot: ...

    @property
    def artifacts(self) -> tuple[ArtifactSnapshot, ...]: ...

    @property
    def sha256(self) -> str: ...


class HostReader(Protocol):
    """按身份读取推理机并分配稳定配置修订号。"""

    def by_id(self, host_id: UUID, /) -> HostSnapshot | None:
        """返回推理机快照；不存在时返回 `None`。"""
        ...

    def next_configuration_revision(
        self, *, host_id: UUID, content_sha256: str, minimum_revision: int = 0
    ) -> int:
        """为新的有效配置内容分配单调修订号。"""
        ...


class BackendReader(Protocol):
    """按主机读取后端分页。"""

    def page_of(
        self, *, page: int, page_size: int, host_id: UUID | None
    ) -> tuple[Sequence[BackendSnapshot], int]:
        """返回指定主机的一页后端及稳定总数。"""
        ...


class CameraReader(Protocol):
    """读取主机上的相机拓扑。"""

    def for_host(self, host_id: UUID, /) -> Sequence[CameraSnapshot]:
        """返回主机上的全部相机。"""
        ...


class StationReader(Protocol):
    """按身份读取工位。"""

    def by_id(self, station_id: UUID, /) -> StationSnapshot | None:
        """返回工位快照；不存在时返回 `None`。"""
        ...


class ConnectorReader(Protocol):
    """读取工位上的连接器。"""

    def for_station(self, station_id: UUID, /) -> Sequence[ConnectorSnapshot]:
        """返回工位上的全部连接器。"""
        ...


class PointReader(Protocol):
    """读取工位上的点位。"""

    def for_station(self, station_id: UUID, /) -> Sequence[PointSnapshot]:
        """返回工位上的全部点位。"""
        ...


class TemplateReader(Protocol):
    """读取工位绑定和已发布模板版本。"""

    def binding_by_station(self, station_id: UUID, /) -> TemplateBindingSnapshot | None:
        """返回工位期望绑定；不存在时返回 `None`。"""
        ...

    def version_by_id(self, version_id: UUID, /) -> TemplateVersionSnapshot | None:
        """返回已发布版本；不存在时返回 `None`。"""
        ...

    def template_by_id(self, template_id: UUID, /) -> object | None:
        """返回模板身份；不存在时返回 `None`。"""
        ...


__all__ = [
    "BackendReader",
    "BackendSnapshot",
    "CameraReader",
    "CameraSnapshot",
    "ConnectorConfigurationSnapshot",
    "ConnectorReader",
    "ConnectorSnapshot",
    "EnumSnapshot",
    "HostReader",
    "HostSnapshot",
    "PointReader",
    "PointSnapshot",
    "RuntimeDefaultsSnapshot",
    "RuntimeParametersSnapshot",
    "StationReader",
    "StationSnapshot",
    "TemplateBindingSnapshot",
    "TemplateReader",
    "TemplateVersionSnapshot",
]
