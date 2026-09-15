"""template 模块对其他模块暴露的最小跨模块接口。"""

from __future__ import annotations

from factory_sop.auth.api import Caller
from factory_sop.template.repository import TemplateRepository


def summary(*, caller: Caller, templates: TemplateRepository) -> dict[str, object]:
    """返回 overview 使用的权限裁剪模板摘要。"""
    from factory_sop.template.usecases.summary import summary as build_summary

    return build_summary(caller=caller, templates=templates)


__all__ = ["summary"]
