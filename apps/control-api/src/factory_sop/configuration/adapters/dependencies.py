"""机器配置 HTTP adapter 的跨 owner 注入点；真实 wiring 只在 app composition root。"""

from __future__ import annotations

from factory_sop.device.api import DeviceConfigurationGateway
from factory_sop.template.api import TemplateConfigurationGateway


def device_gateway() -> DeviceConfigurationGateway:
    """由 composition root 注入 device owner 的配置 seam。"""
    raise RuntimeError("configuration device gateway dependency was not wired")


def template_gateway() -> TemplateConfigurationGateway:
    """由 composition root 注入 template owner 的配置 seam。"""
    raise RuntimeError("configuration template gateway dependency was not wired")


__all__ = ["device_gateway", "template_gateway"]
