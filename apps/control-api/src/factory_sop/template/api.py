"""template 模块对其他模块暴露的最小跨模块接口。

角色：组合根通过本模块入口读取模板摘要；调用方提供已认证 ``Caller`` 和请求级
repository，权限裁剪仍由 template usecase 负责。
"""

from __future__ import annotations

from factory_sop.auth.api import Caller
from factory_sop.template.repository import TemplateRepository


def summary(*, caller: Caller, templates: TemplateRepository) -> dict[str, object]:
    """返回 overview 使用的模板摘要; caller 权限由 template 用例负责裁剪。"""
    from factory_sop.template.usecases.summary import summary as build_summary

    return build_summary(caller=caller, templates=templates)


__all__ = ["summary"]
