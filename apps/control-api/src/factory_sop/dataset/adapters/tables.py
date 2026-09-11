"""`dataset` 的训练数据集、视频成员和标注生命周期持久化行。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from factory_sop.dataset.model import (
    ActionListRevision,
    AnnotationContext,
    AnnotationExecution,
    AnnotationExecutionStatus,
    AnnotationMode,
    AnnotationPreparationStatus,
    AnnotationSegment,
    AnnotationSubmission,
    DatasetMember,
    TrainingDataset,
    UploadAttempt,
)
from factory_sop.persistence import Table


class TrainingDatasetRow(Table):
    """训练数据集分组行。"""

    __tablename__ = "dataset_training_dataset"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    created_by: Mapped[UUID] = mapped_column(Uuid())
    updated_by: Mapped[UUID] = mapped_column(Uuid())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_domain(self) -> TrainingDataset:
        return TrainingDataset(
            id=self.id,
            name=self.name,
            created_by=self.created_by,
            updated_by=self.updated_by,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_domain(cls, dataset: TrainingDataset) -> TrainingDatasetRow:
        return cls(
            id=dataset.id,
            name=dataset.name,
            created_by=dataset.created_by,
            updated_by=dataset.updated_by,
            created_at=dataset.created_at,
            updated_at=dataset.updated_at,
        )


class DatasetMemberRow(Table):
    """数据集内单个视频成员行；声明和验证事实分列保存。"""

    __tablename__ = "dataset_member"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    dataset_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("dataset_training_dataset.id", ondelete="CASCADE"), index=True
    )
    original_filename: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(255))
    declared_size: Mapped[int] = mapped_column(BigInteger())
    declared_sha256: Mapped[str] = mapped_column(String(64))
    current_attempt_id: Mapped[UUID] = mapped_column(Uuid(), index=True)
    status: Mapped[str] = mapped_column(String(32))
    actual_size: Mapped[int | None] = mapped_column(BigInteger())
    actual_sha256: Mapped[str | None] = mapped_column(String(64))
    duration_seconds: Mapped[float | None] = mapped_column(Float())
    codec: Mapped[str | None] = mapped_column(String(64))
    container: Mapped[str | None] = mapped_column(String(128))
    object_key: Mapped[str | None] = mapped_column(String(512))
    object_version_id: Mapped[str | None] = mapped_column(String(255))
    validation_job_id: Mapped[UUID | None] = mapped_column(Uuid(), index=True)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    failure_detail: Mapped[str | None] = mapped_column(String(1024))
    recovery_action: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[UUID] = mapped_column(Uuid())
    updated_by: Mapped[UUID] = mapped_column(Uuid())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_domain(self) -> DatasetMember:
        return DatasetMember(
            id=self.id,
            dataset_id=self.dataset_id,
            original_filename=self.original_filename,
            source=self.source,
            declared_size=self.declared_size,
            declared_sha256=self.declared_sha256,
            current_attempt_id=self.current_attempt_id,
            status=self.status,
            actual_size=self.actual_size,
            actual_sha256=self.actual_sha256,
            duration_seconds=self.duration_seconds,
            codec=self.codec,
            container=self.container,
            object_key=self.object_key,
            object_version_id=self.object_version_id,
            validation_job_id=self.validation_job_id,
            failure_code=self.failure_code,
            failure_detail=self.failure_detail,
            recovery_action=self.recovery_action,
            created_by=self.created_by,
            updated_by=self.updated_by,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_domain(cls, member: DatasetMember) -> DatasetMemberRow:
        return cls(
            id=member.id,
            dataset_id=member.dataset_id,
            original_filename=member.original_filename,
            source=member.source,
            declared_size=member.declared_size,
            declared_sha256=member.declared_sha256,
            current_attempt_id=member.current_attempt_id,
            status=member.status,
            actual_size=member.actual_size,
            actual_sha256=member.actual_sha256,
            duration_seconds=member.duration_seconds,
            codec=member.codec,
            container=member.container,
            object_key=member.object_key,
            object_version_id=member.object_version_id,
            validation_job_id=member.validation_job_id,
            failure_code=member.failure_code,
            failure_detail=member.failure_detail,
            recovery_action=member.recovery_action,
            created_by=member.created_by,
            updated_by=member.updated_by,
            created_at=member.created_at,
            updated_at=member.updated_at,
        )


class UploadAttemptRow(Table):
    """每次上传拥有独立对象 key；幂等键只在数据集内生效。"""

    __tablename__ = "dataset_upload_attempt"
    __table_args__ = (UniqueConstraint("dataset_id", "idempotency_key"),)

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    dataset_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("dataset_training_dataset.id", ondelete="CASCADE"), index=True
    )
    member_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("dataset_member.id", ondelete="CASCADE"), index=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    object_key: Mapped[str] = mapped_column(String(512), unique=True)
    declared_size: Mapped[int] = mapped_column(BigInteger())
    declared_sha256: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    validation_job_id: Mapped[UUID | None] = mapped_column(Uuid())
    object_version_id: Mapped[str | None] = mapped_column(String(255))

    def to_domain(self) -> UploadAttempt:
        return UploadAttempt(
            id=self.id,
            dataset_id=self.dataset_id,
            member_id=self.member_id,
            idempotency_key=self.idempotency_key,
            object_key=self.object_key,
            declared_size=self.declared_size,
            declared_sha256=self.declared_sha256,
            expires_at=self.expires_at,
            status=self.status,
            created_at=self.created_at,
            validation_job_id=self.validation_job_id,
            object_version_id=self.object_version_id,
        )

    @classmethod
    def from_domain(cls, attempt: UploadAttempt) -> UploadAttemptRow:
        return cls(
            id=attempt.id,
            dataset_id=attempt.dataset_id,
            member_id=attempt.member_id,
            idempotency_key=attempt.idempotency_key,
            object_key=attempt.object_key,
            declared_size=attempt.declared_size,
            declared_sha256=attempt.declared_sha256,
            expires_at=attempt.expires_at,
            status=attempt.status,
            created_at=attempt.created_at,
            validation_job_id=attempt.validation_job_id,
            object_version_id=attempt.object_version_id,
        )


class ActionListRevisionRow(Table):
    """动作清单的不可变修订行。"""

    __tablename__ = "dataset_action_list_revision"
    __table_args__ = (UniqueConstraint("dataset_id", "revision"),)

    dataset_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("dataset_training_dataset.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer(), primary_key=True)
    actions: Mapped[list[str]] = mapped_column(JSONB())
    created_by: Mapped[UUID] = mapped_column(Uuid())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_domain(self) -> ActionListRevision:
        return ActionListRevision(
            dataset_id=self.dataset_id,
            revision=self.revision,
            actions=tuple(self.actions),
            created_by=self.created_by,
            created_at=self.created_at,
        )

    @classmethod
    def from_domain(cls, value: ActionListRevision) -> ActionListRevisionRow:
        return cls(
            dataset_id=value.dataset_id,
            revision=value.revision,
            actions=list(value.actions),
            created_by=value.created_by,
            created_at=value.created_at,
        )


class AnnotationContextRow(Table):
    """标注上下文及其基座工作副本身份。"""

    __tablename__ = "dataset_annotation_context"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    dataset_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("dataset_training_dataset.id", ondelete="CASCADE")
    )
    member_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("dataset_member.id", ondelete="CASCADE"), index=True
    )
    action_list_revision: Mapped[int] = mapped_column(Integer())
    annotation_revision: Mapped[int] = mapped_column(Integer())
    source_object_version_id: Mapped[str] = mapped_column(String(255))
    source_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[UUID] = mapped_column(Uuid())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    upstream_data_id: Mapped[str | None] = mapped_column(String(255))
    upstream_video_id: Mapped[str | None] = mapped_column(String(255))
    upstream_video_size: Mapped[int | None] = mapped_column(BigInteger())
    upstream_video_sha256: Mapped[str | None] = mapped_column(String(64))
    upstream_video_duration_seconds: Mapped[float | None] = mapped_column(Float())
    preparation_job_id: Mapped[UUID | None] = mapped_column(Uuid(), index=True)
    preparation_status: Mapped[str] = mapped_column(
        String(32), default=AnnotationPreparationStatus.PENDING.value
    )
    preparation_failure_code: Mapped[str | None] = mapped_column(String(64))
    preparation_failure_detail: Mapped[str | None] = mapped_column(String(1024))

    def to_domain(self) -> AnnotationContext:
        return AnnotationContext(
            id=self.id,
            dataset_id=self.dataset_id,
            member_id=self.member_id,
            action_list_revision=self.action_list_revision,
            annotation_revision=self.annotation_revision,
            source_object_version_id=self.source_object_version_id,
            source_sha256=self.source_sha256,
            created_by=self.created_by,
            created_at=self.created_at,
            expires_at=self.expires_at,
            upstream_data_id=self.upstream_data_id,
            upstream_video_id=self.upstream_video_id,
            upstream_video_size=self.upstream_video_size,
            upstream_video_sha256=self.upstream_video_sha256,
            upstream_video_duration_seconds=self.upstream_video_duration_seconds,
            preparation_job_id=self.preparation_job_id,
            preparation_status=self.preparation_status,
            preparation_failure_code=self.preparation_failure_code,
            preparation_failure_detail=self.preparation_failure_detail,
        )

    @classmethod
    def from_domain(cls, value: AnnotationContext) -> AnnotationContextRow:
        return cls(
            id=value.id,
            dataset_id=value.dataset_id,
            member_id=value.member_id,
            action_list_revision=value.action_list_revision,
            annotation_revision=value.annotation_revision,
            source_object_version_id=value.source_object_version_id,
            source_sha256=value.source_sha256,
            created_by=value.created_by,
            created_at=value.created_at,
            expires_at=value.expires_at,
            upstream_data_id=value.upstream_data_id,
            upstream_video_id=value.upstream_video_id,
            upstream_video_size=value.upstream_video_size,
            upstream_video_sha256=value.upstream_video_sha256,
            upstream_video_duration_seconds=value.upstream_video_duration_seconds,
            preparation_job_id=value.preparation_job_id,
            preparation_status=value.preparation_status,
            preparation_failure_code=value.preparation_failure_code,
            preparation_failure_detail=value.preparation_failure_detail,
        )


class AnnotationSubmissionRow(Table):
    """稳定业务提交及其原始时间段输入。"""

    __tablename__ = "dataset_annotation_submission"
    __table_args__ = (
        UniqueConstraint("dataset_id", "idempotency_key"),
        UniqueConstraint("member_id", "revision"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    dataset_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("dataset_training_dataset.id", ondelete="CASCADE")
    )
    member_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("dataset_member.id", ondelete="CASCADE"), index=True
    )
    context_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("dataset_annotation_context.id", ondelete="CASCADE")
    )
    revision: Mapped[int] = mapped_column(Integer())
    action_list_revision: Mapped[int] = mapped_column(Integer())
    source_object_version_id: Mapped[str] = mapped_column(String(255))
    source_sha256: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    request_digest: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(32))
    segments: Mapped[list[dict[str, Any]]] = mapped_column(JSONB())
    raw_segments: Mapped[list[dict[str, Any]]] = mapped_column(JSONB())
    created_by: Mapped[UUID] = mapped_column(Uuid())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_domain(self) -> AnnotationSubmission:
        return AnnotationSubmission(
            id=self.id,
            dataset_id=self.dataset_id,
            member_id=self.member_id,
            context_id=self.context_id,
            revision=self.revision,
            action_list_revision=self.action_list_revision,
            source_object_version_id=self.source_object_version_id,
            source_sha256=self.source_sha256,
            idempotency_key=self.idempotency_key,
            request_digest=self.request_digest,
            mode=AnnotationMode(self.mode),
            segments=tuple(
                AnnotationSegment(
                    start=float(item["start"]),
                    end=float(item["end"]),
                    action_index=int(item["actionIndex"]),
                    action_description=str(item["actionDescription"]),
                )
                for item in self.segments
            ),
            raw_segments=tuple(self.raw_segments),
            created_by=self.created_by,
            created_at=self.created_at,
        )

    @classmethod
    def from_domain(cls, value: AnnotationSubmission) -> AnnotationSubmissionRow:
        return cls(
            id=value.id,
            dataset_id=value.dataset_id,
            member_id=value.member_id,
            context_id=value.context_id,
            revision=value.revision,
            action_list_revision=value.action_list_revision,
            source_object_version_id=value.source_object_version_id,
            source_sha256=value.source_sha256,
            idempotency_key=value.idempotency_key,
            request_digest=value.request_digest,
            mode=value.mode.value,
            segments=[segment.as_wire() for segment in value.segments],
            raw_segments=list(value.raw_segments),
            created_by=value.created_by,
            created_at=value.created_at,
        )


class AnnotationExecutionRow(Table):
    """一个提交的执行候选代次；候选结果完成后只追加新代次。"""

    __tablename__ = "dataset_annotation_execution"
    __table_args__ = (
        UniqueConstraint("submission_id", "generation"),
        UniqueConstraint("job_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    submission_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("dataset_annotation_submission.id", ondelete="CASCADE"), index=True
    )
    generation: Mapped[int] = mapped_column(Integer())
    job_id: Mapped[UUID | None] = mapped_column(Uuid())
    status: Mapped[str] = mapped_column(String(32))
    clips: Mapped[list[dict[str, Any]]] = mapped_column(JSONB())
    failure_code: Mapped[str | None] = mapped_column(String(64))
    failure_detail: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    upstream_data_id: Mapped[str | None] = mapped_column(String(255))
    upstream_video_id: Mapped[str | None] = mapped_column(String(255))
    derived_video_size: Mapped[int | None] = mapped_column(BigInteger())
    derived_video_sha256: Mapped[str | None] = mapped_column(String(64))
    derived_video_duration_seconds: Mapped[float | None] = mapped_column(Float())

    def to_domain(self) -> AnnotationExecution:
        return AnnotationExecution(
            id=self.id,
            submission_id=self.submission_id,
            generation=self.generation,
            job_id=self.job_id,
            status=AnnotationExecutionStatus(self.status),
            clips=tuple(self.clips),
            failure_code=self.failure_code,
            failure_detail=self.failure_detail,
            created_at=self.created_at,
            updated_at=self.updated_at,
            upstream_data_id=self.upstream_data_id,
            upstream_video_id=self.upstream_video_id,
            derived_video_size=self.derived_video_size,
            derived_video_sha256=self.derived_video_sha256,
            derived_video_duration_seconds=self.derived_video_duration_seconds,
        )

    @classmethod
    def from_domain(cls, value: AnnotationExecution) -> AnnotationExecutionRow:
        return cls(
            id=value.id,
            submission_id=value.submission_id,
            generation=value.generation,
            job_id=value.job_id,
            status=value.status,
            clips=list(value.clips),
            failure_code=value.failure_code,
            failure_detail=value.failure_detail,
            created_at=value.created_at,
            updated_at=value.updated_at,
            upstream_data_id=value.upstream_data_id,
            upstream_video_id=value.upstream_video_id,
            derived_video_size=value.derived_video_size,
            derived_video_sha256=value.derived_video_sha256,
            derived_video_duration_seconds=value.derived_video_duration_seconds,
        )
