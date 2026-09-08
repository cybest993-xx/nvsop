"""`template` 用例访问持久化的唯一 seam。"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from factory_sop.template.model import (
    SopTemplate,
    TemplateDraft,
    TemplateDraftDocument,
    TemplateImport,
)


class TemplateRepository(Protocol):
    """`template_sop`、`template_draft` 和 `template_import` 的组合仓储。"""

    def add_import(self, record: TemplateImport) -> None:
        """保存原始文档及导入结果；不提交事务。"""
        ...

    def add_template(self, template: SopTemplate) -> None:
        """保存一个由本次成功导入创建的模板身份；不提交事务。"""
        ...

    def add_draft(self, draft: TemplateDraft) -> None:
        """保存一个新草稿；不提交事务。"""
        ...

    def save_draft(self, draft: TemplateDraft, *, expected_revision: int) -> None:
        """按调用方读取的 revision 替换草稿，否则拒绝覆盖。"""
        ...

    def draft_by_id(self, draft_id: UUID) -> TemplateDraftDocument | None:
        """按公开 UUID 返回草稿及其模板身份。"""
        ...

    def page_drafts(self, *, page: int, page_size: int) -> tuple[list[TemplateDraftDocument], int]:
        """返回按更新时间倒序的一页草稿和总数。"""
        ...

    def import_by_id(self, import_id: UUID) -> TemplateImport | None:
        """按公开 UUID 返回一条导入记录。"""
        ...

    def page_imports(self, *, page: int, page_size: int) -> tuple[list[TemplateImport], int]:
        """返回按导入时间倒序的一页导入记录和总数。"""
        ...
