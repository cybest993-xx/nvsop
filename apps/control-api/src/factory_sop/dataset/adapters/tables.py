"""`dataset` 的训练数据集、视频成员和上传尝试持久化行。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from factory_sop.dataset.model import DatasetMember, TrainingDataset, UploadAttempt
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
