"""模板模块的 HTTP 依赖。"""

from __future__ import annotations

from factory_sop.device.api import (
    DeviceHostGateway,
    DeviceTemplateBindingGateway,
    StationCodeLookup,
)
from factory_sop.persistence import RequestSession
from factory_sop.template.adapters.repository import PostgresTemplateRepository
from factory_sop.template.repository import TemplateRepository


def templates(
    session: RequestSession,
) -> TemplateRepository:
    """返回请求事务中的 `template_*` 行。"""
    return PostgresTemplateRepository(session)


def stations() -> StationCodeLookup:
    """由组合根注入 `device` 的自然编码查询 seam。"""
    raise RuntimeError("template station lookup dependency was not wired")


def binding_gateway() -> DeviceTemplateBindingGateway:
    """由组合根注入 `device` 的拓扑与工位参数 seam。"""
    raise RuntimeError("template binding gateway dependency was not wired")


def host_gateway() -> DeviceHostGateway:
    """由组合根注入 `device` 的主机认证与归属 seam。"""
    raise RuntimeError("template host gateway dependency was not wired")
