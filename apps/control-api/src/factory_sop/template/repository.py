"""`template` 用例访问持久化的唯一 seam。"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from factory_sop.template.model import (
    SopTemplate,
    TemplateArtifactName,
    TemplateDraft,
    TemplateDraftDocument,
    TemplateImport,
    TemplateVersion,
    TemplateVersionArtifact,
    TemplateVersionWriteResult,
)


class TemplateDraftRepository(Protocol):
    """草稿导入、编辑和读取用例所跨的最小 seam。"""

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


class TemplateVersionPublishRepository(Protocol):
    """模板版本发布用例所跨的最小 seam。"""

    def draft_by_id(self, draft_id: UUID) -> TemplateDraftDocument | None:
        """按草稿身份读取发布所需的完整修订快照。"""
        ...

    def draft_for_publish(self, draft_id: UUID) -> TemplateDraftDocument | None:
        """锁定草稿行并读取发布所需的完整修订快照。"""
        ...

    def version_by_source(self, *, draft_id: UUID, revision: int) -> TemplateVersion | None:
        """按草稿身份和修订号读取已发布版本，供发布重试幂等返回。"""
        ...

    def add_version(self, version: TemplateVersion) -> TemplateVersionWriteResult:
        """保存不可变版本；并发重复发布返回已存在的版本。"""
        ...


class TemplateVersionRepository(Protocol):
    """模板版本历史读取和制品下载所跨的最小 seam。"""

    def version_by_id(self, version_id: UUID) -> TemplateVersion | None:
        """按公开 UUID 读取完整版本及其制品。"""
        ...

    def page_versions(self, *, page: int, page_size: int) -> tuple[list[TemplateVersion], int]:
        """返回按发布时间倒序的一页不可变版本。"""
        ...

    def artifact_by_name(
        self, *, version_id: UUID, name: TemplateArtifactName
    ) -> TemplateVersionArtifact | None:
        """按版本身份和允许的制品名称读取已保存字节。"""
        ...


class TemplateRepository(
    TemplateDraftRepository,
    TemplateVersionPublishRepository,
    TemplateVersionRepository,
    Protocol,
):
    """`template_sop`、`template_draft` 和 `template_version` 的组合仓储。"""
