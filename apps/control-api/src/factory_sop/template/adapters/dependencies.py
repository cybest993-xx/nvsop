"""模板模块的 HTTP 依赖。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.device.api import StationCodeLookup
from factory_sop.persistence import request_session
from factory_sop.template.adapters.repository import PostgresTemplateRepository
from factory_sop.template.repository import TemplateRepository


def templates(
    session: Annotated[DatabaseSession, Depends(request_session)],
) -> TemplateRepository:
    """返回请求事务中的 `template_*` 行。"""
    return PostgresTemplateRepository(session)


def stations() -> StationCodeLookup:
    """由组合根注入 `device` 的自然编码查询 seam。"""
    raise RuntimeError("template station lookup dependency was not wired")
