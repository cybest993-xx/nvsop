"""configuration 模块对跨模块调用方暴露的配置组装接缝。

组合根负责把真实仓储和主机认证实现装配到这里；配置 HTTP 适配器只依赖本模块的
``ConfigurationSources``，不会读取 device 或 template 的适配器实现。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from factory_sop.configuration.repository import (
    BackendReader,
    CameraReader,
    ConnectorReader,
    HostReader,
    PointReader,
    StationReader,
    TemplateReader,
)
from factory_sop.configuration.usecases import ConfigurationAssemblyError, configuration_for_host
from factory_sop.persistence import RequestSession


@dataclass(frozen=True, slots=True)
class ConfigurationSources:
    """配置组装所需的读取和认证接口。"""

    authenticate: Callable[..., None]
    hosts: Callable[[RequestSession], HostReader]
    backends: Callable[[RequestSession], BackendReader]
    stations: Callable[[RequestSession], StationReader]
    cameras: Callable[[RequestSession], CameraReader]
    connectors: Callable[[RequestSession], ConnectorReader]
    points: Callable[[RequestSession], PointReader]
    templates: Callable[[RequestSession], TemplateReader]


__all__ = [
    "ConfigurationAssemblyError",
    "ConfigurationSources",
    "configuration_for_host",
]
