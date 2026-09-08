"""`template` 模块的导入、SOP 模板身份和可编辑草稿持久化行。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from factory_sop.persistence import Table
from factory_sop.template.errors import TemplateFieldError
from factory_sop.template.model import (
    ImportStatus,
    OrderingMode,
    SopTemplate,
    TemplateDraft,
    TemplateImport,
    TemplateRuntimeDefaults,
    TemplateStep,
)


def _import_status_enum(*, create_type: bool = True) -> Enum:
    return Enum(
        ImportStatus,
        name="template_import_status",
        create_type=create_type,
        values_callable=lambda enum: [member.value for member in enum],
    )


def _ordering_enum(*, create_type: bool = True) -> Enum:
    return Enum(
        OrderingMode,
        name="template_ordering",
        create_type=create_type,
        values_callable=lambda enum: [member.value for member in enum],
    )


class TemplateImportRow(Table):
    """原始导入文件及其可定位的校验结果；失败记录也必须保留。"""

    __tablename__ = "template_import"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(255))
    original_document: Mapped[bytes] = mapped_column(LargeBinary())
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[ImportStatus] = mapped_column(_import_status_enum())
    errors: Mapped[list[dict[str, object]]] = mapped_column(JSONB())
    imported_by: Mapped[UUID] = mapped_column(Uuid())
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_domain(self) -> TemplateImport:
        return TemplateImport(
            id=self.id,
            filename=self.filename,
            content_type=self.content_type,
            original_document=bytes(self.original_document),
            sha256=self.sha256,
            status=self.status,
            errors=tuple(TemplateFieldError.from_wire(error) for error in self.errors),
            imported_by=self.imported_by,
            imported_at=self.imported_at,
        )

    @classmethod
    def from_domain(cls, record: TemplateImport) -> TemplateImportRow:
        return cls(
            id=record.id,
            filename=record.filename,
            content_type=record.content_type,
            original_document=record.original_document,
            sha256=record.sha256,
            status=record.status,
            errors=[error.to_wire() for error in record.errors],
            imported_by=record.imported_by,
            imported_at=record.imported_at,
        )


class SopTemplateRow(Table):
    """已匹配设备工位的 SOP 模板身份。"""

    __tablename__ = "template_sop_template"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    station_id: Mapped[UUID] = mapped_column(Uuid(), ForeignKey("device_station.id"), index=True)
    station_code: Mapped[str] = mapped_column(String(64))
    station_name: Mapped[str] = mapped_column(String(128))
    created_by: Mapped[UUID] = mapped_column(Uuid())
    updated_by: Mapped[UUID] = mapped_column(Uuid())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_domain(self) -> SopTemplate:
        return SopTemplate(
            id=self.id,
            station_id=self.station_id,
            station_code=self.station_code,
            station_name=self.station_name,
            created_by=self.created_by,
            updated_by=self.updated_by,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_domain(cls, template: SopTemplate) -> SopTemplateRow:
        return cls(
            id=template.id,
            station_id=template.station_id,
            station_code=template.station_code,
            station_name=template.station_name,
            created_by=template.created_by,
            updated_by=template.updated_by,
            created_at=template.created_at,
            updated_at=template.updated_at,
        )


class TemplateDraftRow(Table):
    """可编辑但尚未发布的模板草稿。"""

    __tablename__ = "template_draft"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    template_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("template_sop_template.id"), index=True
    )
    source_import_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("template_import.id"), index=True
    )
    steps: Mapped[list[dict[str, object]]] = mapped_column(JSONB())
    ordering: Mapped[OrderingMode] = mapped_column(_ordering_enum())
    runtime_defaults: Mapped[dict[str, object]] = mapped_column(JSONB())
    revision: Mapped[int] = mapped_column(Integer())
    created_by: Mapped[UUID] = mapped_column(Uuid())
    updated_by: Mapped[UUID] = mapped_column(Uuid())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_domain(self) -> TemplateDraft:
        return TemplateDraft(
            id=self.id,
            template_id=self.template_id,
            source_import_id=self.source_import_id,
            steps=tuple(TemplateStep.from_wire(step) for step in self.steps),
            ordering=self.ordering,
            runtime_defaults=TemplateRuntimeDefaults.from_wire(self.runtime_defaults),
            revision=self.revision,
            created_by=self.created_by,
            updated_by=self.updated_by,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_domain(cls, draft: TemplateDraft) -> TemplateDraftRow:
        return cls(
            id=draft.id,
            template_id=draft.template_id,
            source_import_id=draft.source_import_id,
            steps=[step.to_wire() for step in draft.steps],
            ordering=draft.ordering,
            runtime_defaults=draft.runtime_defaults.to_wire(),
            revision=draft.revision,
            created_by=draft.created_by,
            updated_by=draft.updated_by,
            created_at=draft.created_at,
            updated_at=draft.updated_at,
        )
