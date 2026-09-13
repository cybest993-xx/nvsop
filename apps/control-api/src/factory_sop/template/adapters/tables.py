"""`template` 模块的导入、SOP 模板身份和可编辑草稿持久化行。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
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
    TemplateArtifactName,
    TemplateBoundaryDraft,
    TemplateConfigurationReport,
    TemplateDraft,
    TemplateImport,
    TemplateReportRejectionCode,
    TemplateRuntimeDefaults,
    TemplateStationBinding,
    TemplateStep,
    TemplateVersion,
    TemplateVersionArtifact,
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


def _report_rejection_enum(*, create_type: bool = True) -> Enum:
    return Enum(
        TemplateReportRejectionCode,
        name="template_report_rejection_code",
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
            errors=tuple(TemplateFieldError.from_wire(error) for error in deepcopy(self.errors)),
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
            errors=[deepcopy(error.to_wire()) for error in record.errors],
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
    boundary: Mapped[dict[str, object] | None] = mapped_column(JSONB(), nullable=True)
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
            steps=tuple(TemplateStep.from_wire(step) for step in deepcopy(self.steps)),
            ordering=self.ordering,
            runtime_defaults=TemplateRuntimeDefaults.from_wire(deepcopy(self.runtime_defaults)),
            revision=self.revision,
            boundary=(
                None
                if self.boundary is None
                else TemplateBoundaryDraft.from_wire(deepcopy(self.boundary))
            ),
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
            boundary=draft.boundary.to_wire() if draft.boundary is not None else None,
            revision=draft.revision,
            created_by=draft.created_by,
            updated_by=draft.updated_by,
            created_at=draft.created_at,
            updated_at=draft.updated_at,
        )


class TemplateVersionRow(Table):
    """不可变模板版本和发布时保存的确定性制品。"""

    __tablename__ = "template_version"
    __table_args__ = (
        UniqueConstraint(
            "source_draft_id",
            "source_draft_revision",
            name="uq_template_version_source_draft_revision",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    template_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("template_sop_template.id"), index=True
    )
    source_import_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("template_import.id"), index=True
    )
    source_draft_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("template_draft.id"), index=True
    )
    source_draft_revision: Mapped[int] = mapped_column(Integer())
    steps: Mapped[list[dict[str, object]]] = mapped_column(JSONB())
    ordering: Mapped[OrderingMode] = mapped_column(_ordering_enum(create_type=False))
    boundary: Mapped[dict[str, object]] = mapped_column(JSONB())
    runtime_defaults: Mapped[dict[str, object]] = mapped_column(JSONB())
    actions_json: Mapped[bytes] = mapped_column(LargeBinary())
    actions_sha256: Mapped[str] = mapped_column(String(64))
    vlm_prompts: Mapped[bytes] = mapped_column(LargeBinary())
    vlm_prompts_sha256: Mapped[str] = mapped_column(String(64))
    template_json: Mapped[bytes] = mapped_column(LargeBinary())
    template_sha256: Mapped[str] = mapped_column(String(64))
    manifest_json: Mapped[bytes] = mapped_column(LargeBinary())
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    sha256: Mapped[str] = mapped_column(String(64))
    published_by: Mapped[UUID] = mapped_column(Uuid())
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_domain(self) -> TemplateVersion:
        artifacts = (
            TemplateVersionArtifact(
                name=TemplateArtifactName.ACTIONS,
                media_type="application/json",
                content=bytes(self.actions_json),
                sha256=self.actions_sha256,
            ),
            TemplateVersionArtifact(
                name=TemplateArtifactName.VLM_PROMPTS,
                media_type="text/plain",
                content=bytes(self.vlm_prompts),
                sha256=self.vlm_prompts_sha256,
            ),
            TemplateVersionArtifact(
                name=TemplateArtifactName.TEMPLATE,
                media_type="application/json",
                content=bytes(self.template_json),
                sha256=self.template_sha256,
            ),
            TemplateVersionArtifact(
                name=TemplateArtifactName.MANIFEST,
                media_type="application/json",
                content=bytes(self.manifest_json),
                sha256=self.manifest_sha256,
            ),
        )
        return TemplateVersion(
            id=self.id,
            template_id=self.template_id,
            source_import_id=self.source_import_id,
            source_draft_id=self.source_draft_id,
            source_draft_revision=self.source_draft_revision,
            steps=tuple(TemplateStep.from_wire(step) for step in deepcopy(self.steps)),
            ordering=self.ordering,
            boundary=TemplateBoundaryDraft.from_wire(deepcopy(self.boundary)),
            runtime_defaults=TemplateRuntimeDefaults.from_wire(deepcopy(self.runtime_defaults)),
            artifacts=artifacts,
            sha256=self.sha256,
            published_by=self.published_by,
            published_at=self.published_at,
        )

    @staticmethod
    def values_from_domain(version: TemplateVersion) -> dict[str, object]:
        artifacts = {artifact.name: artifact for artifact in version.artifacts}
        return {
            "id": version.id,
            "template_id": version.template_id,
            "source_import_id": version.source_import_id,
            "source_draft_id": version.source_draft_id,
            "source_draft_revision": version.source_draft_revision,
            "steps": [step.to_wire() for step in version.steps],
            "ordering": version.ordering,
            "boundary": version.boundary.to_wire(),
            "runtime_defaults": version.runtime_defaults.to_wire(),
            "actions_json": artifacts[TemplateArtifactName.ACTIONS].content,
            "actions_sha256": artifacts[TemplateArtifactName.ACTIONS].sha256,
            "vlm_prompts": artifacts[TemplateArtifactName.VLM_PROMPTS].content,
            "vlm_prompts_sha256": artifacts[TemplateArtifactName.VLM_PROMPTS].sha256,
            "template_json": artifacts[TemplateArtifactName.TEMPLATE].content,
            "template_sha256": artifacts[TemplateArtifactName.TEMPLATE].sha256,
            "manifest_json": artifacts[TemplateArtifactName.MANIFEST].content,
            "manifest_sha256": artifacts[TemplateArtifactName.MANIFEST].sha256,
            "sha256": version.sha256,
            "published_by": version.published_by,
            "published_at": version.published_at,
        }

    @classmethod
    def from_domain(cls, version: TemplateVersion) -> TemplateVersionRow:
        return cls(**cls.values_from_domain(version))


class TemplateStationBindingRow(Table):
    """一个工位唯一的期望模板版本及其乐观锁修订。"""

    __tablename__ = "template_station_binding"
    __table_args__ = (
        CheckConstraint(
            "desired_config_revision > 0",
            name="desired_config_revision_positive",
        ),
        CheckConstraint(
            "revision > 0",
            name="revision_positive",
        ),
        CheckConstraint(
            "char_length(desired_sha256) = 64 AND desired_sha256 ~ '^[0-9A-Fa-f]{64}$'",
            name="desired_sha256",
        ),
        UniqueConstraint("station_id", name="uq_template_station_binding_station_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    station_id: Mapped[UUID] = mapped_column(Uuid(), ForeignKey("device_station.id"))
    desired_version_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("template_version.id"), index=True
    )
    desired_sha256: Mapped[str] = mapped_column(String(64))
    desired_config_revision: Mapped[int] = mapped_column(Integer())
    revision: Mapped[int] = mapped_column(Integer())
    created_by: Mapped[UUID] = mapped_column(Uuid())
    updated_by: Mapped[UUID] = mapped_column(Uuid())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_domain(self) -> TemplateStationBinding:
        return TemplateStationBinding(
            id=self.id,
            station_id=self.station_id,
            desired_version_id=self.desired_version_id,
            desired_sha256=self.desired_sha256,
            desired_config_revision=self.desired_config_revision,
            revision=self.revision,
            created_by=self.created_by,
            updated_by=self.updated_by,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @staticmethod
    def values_from_domain(binding: TemplateStationBinding) -> dict[str, object]:
        return {
            "id": binding.id,
            "station_id": binding.station_id,
            "desired_version_id": binding.desired_version_id,
            "desired_sha256": binding.desired_sha256,
            "desired_config_revision": binding.desired_config_revision,
            "revision": binding.revision,
            "created_by": binding.created_by,
            "updated_by": binding.updated_by,
            "created_at": binding.created_at,
            "updated_at": binding.updated_at,
        }

    @classmethod
    def from_domain(cls, binding: TemplateStationBinding) -> TemplateStationBindingRow:
        return cls(**cls.values_from_domain(binding))


class TemplateConfigurationReportRow(Table):
    """按工位/后端保存最后有效确认和最近拒绝事实。"""

    __tablename__ = "template_configuration_report"
    __table_args__ = (
        CheckConstraint(
            "(reported_version_id IS NULL AND reported_sha256 IS NULL "
            "AND reported_config_revision IS NULL AND reported_at IS NULL) OR "
            "(reported_version_id IS NOT NULL AND reported_sha256 IS NOT NULL "
            "AND reported_config_revision IS NOT NULL AND reported_at IS NOT NULL)",
            name="ck_template_configuration_report_reported_fields_complete",
        ),
        CheckConstraint(
            "reported_sha256 IS NULL OR "
            "(char_length(reported_sha256) = 64 AND reported_sha256 ~ '^[0-9A-Fa-f]{64}$')",
            name="ck_template_configuration_report_reported_sha256",
        ),
        CheckConstraint(
            "reported_config_revision IS NULL OR reported_config_revision > 0",
            name="ck_template_configuration_report_reported_config_revision_positive",
        ),
        CheckConstraint(
            "(last_rejection_code IS NULL AND last_rejection_detail IS NULL "
            "AND last_rejection_at IS NULL) OR "
            "(last_rejection_code IS NOT NULL AND last_rejection_detail IS NOT NULL "
            "AND last_rejection_at IS NOT NULL)",
            name="ck_template_configuration_report_rejection_fields_complete",
        ),
        PrimaryKeyConstraint("station_id", "backend_id", name="pk_template_configuration_report"),
        UniqueConstraint(
            "station_id",
            "backend_id",
            name="uq_template_configuration_report_station_id_backend_id",
        ),
    )

    station_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("device_station.id"), primary_key=True
    )
    backend_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("device_inference_backend.id"), primary_key=True
    )
    host_id: Mapped[UUID] = mapped_column(Uuid(), ForeignKey("device_inference_host.id"))
    reported_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(), ForeignKey("template_version.id"), index=True, nullable=True
    )
    reported_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reported_config_revision: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_rejection_code: Mapped[TemplateReportRejectionCode | None] = mapped_column(
        _report_rejection_enum(), nullable=True
    )
    last_rejection_detail: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_rejection_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    updated_by: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_domain(self) -> TemplateConfigurationReport:
        return TemplateConfigurationReport(
            station_id=self.station_id,
            backend_id=self.backend_id,
            host_id=self.host_id,
            reported_version_id=self.reported_version_id,
            reported_sha256=self.reported_sha256,
            reported_config_revision=self.reported_config_revision,
            reported_at=self.reported_at,
            last_rejection_code=self.last_rejection_code,
            last_rejection_detail=self.last_rejection_detail,
            last_rejection_at=self.last_rejection_at,
            created_by=self.created_by,
            updated_by=self.updated_by,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @staticmethod
    def values_from_domain(report: TemplateConfigurationReport) -> dict[str, object]:
        rejection_code = (
            None
            if report.last_rejection_code is None
            else TemplateReportRejectionCode(report.last_rejection_code).value
        )
        return {
            "station_id": report.station_id,
            "backend_id": report.backend_id,
            "host_id": report.host_id,
            "reported_version_id": report.reported_version_id,
            "reported_sha256": report.reported_sha256,
            "reported_config_revision": report.reported_config_revision,
            "reported_at": report.reported_at,
            "last_rejection_code": rejection_code,
            "last_rejection_detail": report.last_rejection_detail,
            "last_rejection_at": report.last_rejection_at,
            "created_by": report.created_by,
            "updated_by": report.updated_by,
            "created_at": report.created_at,
            "updated_at": report.updated_at,
        }

    @classmethod
    def from_domain(cls, report: TemplateConfigurationReport) -> TemplateConfigurationReportRow:
        return cls(**cls.values_from_domain(report))
