"""template 模块对其他模块暴露的最小跨模块接口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from factory_sop.auth.api import Caller
from factory_sop.template.repository import TemplateRepository
from nvsop_contracts import ConfigurationTemplate, ResolvedRuntimeParameters


class TemplateConfigurationError(ValueError):
    """template owner 无法提供一致的机器配置投影。"""


@dataclass(frozen=True, slots=True)
class TemplateConfigurationProjection:
    """配置组合层可见的最小模板事实。"""

    template: ConfigurationTemplate | None
    runtime_defaults: ResolvedRuntimeParameters | None
    revision: int


class TemplateConfigurationGateway(Protocol):
    """机器配置组合只可通过的 template owner 接缝。"""

    def for_station(self, station_id: UUID) -> TemplateConfigurationProjection:
        """返回工位当前期望绑定、制品和运行参数默认值。"""
        ...


def summary(*, caller: Caller, templates: TemplateRepository) -> dict[str, object]:
    """返回 overview 使用的权限裁剪模板摘要。"""
    from factory_sop.template.usecases.summary import summary as build_summary

    return build_summary(caller=caller, templates=templates)


__all__ = [
    "TemplateConfigurationError",
    "TemplateConfigurationGateway",
    "TemplateConfigurationProjection",
    "summary",
]
