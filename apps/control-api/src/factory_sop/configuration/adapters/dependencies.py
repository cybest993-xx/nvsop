"""configuration HTTP 适配器的组合根依赖。"""

from factory_sop.configuration.api import ConfigurationSources


def sources() -> ConfigurationSources:
    """返回由组合根装配的配置读取接口。"""
    raise RuntimeError("configuration sources were not wired")


__all__ = ["sources"]
