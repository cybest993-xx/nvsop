"""template 模块拥有的权限裁剪配置摘要。"""

from __future__ import annotations

import hashlib

from factory_sop.auth.api import Caller, Permission
from factory_sop.summary_support import all_pages, enum_counts
from factory_sop.template.repository import TemplateRepository

Summary = dict[str, object]


def summary(*, caller: Caller, templates: TemplateRepository) -> Summary:
    """返回调用方可见的真实草稿、导入和已发布版本事实。"""
    if not caller.holds(Permission.TEMPLATE_DRAFT_VIEW):
        return {"status": "not_permitted", "data": {}}

    drafts, draft_total = all_pages(
        lambda page, size: templates.page_drafts(page=page, page_size=size)
    )
    imports, import_total = all_pages(
        lambda page, size: templates.page_imports(page=page, page_size=size)
    )
    versions, version_total = all_pages(
        lambda page, size: templates.page_versions(page=page, page_size=size)
    )
    del drafts
    verified_versions = sum(_sha256_verified(value) for value in versions)
    data = {
        "drafts": {"total": draft_total},
        "imports": {
            "total": import_total,
            "by_status": enum_counts(getattr(value, "status", None) for value in imports),
        },
        "published_versions": {
            "total": version_total,
            "sha256_verified": verified_versions,
            "sha256_unverified": version_total - verified_versions,
        },
    }
    status = "available" if draft_total or import_total or version_total else "no_data"
    return {"status": status, "data": data}


def _sha256_verified(value: object) -> bool:
    """按版本清单内容重新计算摘要，避免把非空字符串误当作已校验。"""
    digest = getattr(value, "sha256", None)
    artifacts = getattr(value, "artifacts", ())
    if not isinstance(digest, str) or not artifacts:
        return False
    manifest = artifacts[-1]
    content = getattr(manifest, "content", None)
    return isinstance(content, bytes) and hashlib.sha256(content).hexdigest() == digest


__all__ = ["summary"]
