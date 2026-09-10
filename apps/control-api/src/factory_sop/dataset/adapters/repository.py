"""`dataset` 的 PostgreSQL 仓储适配器。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.dataset.adapters.tables import (
    ActionListRevisionRow,
    AnnotationContextRow,
    AnnotationExecutionRow,
    AnnotationSubmissionRow,
    DatasetMemberRow,
    TrainingDatasetRow,
    UploadAttemptRow,
)
from factory_sop.dataset.model import (
    ActionListRevision,
    AnnotationContext,
    AnnotationExecution,
    AnnotationSubmission,
    DatasetMember,
    TrainingDataset,
    UploadAttempt,
)


class PostgresDatasetRepository:
    """通过请求级会话访问 `dataset_*` 行；任何方法都不提交事务。"""

    def __init__(self, session: DatabaseSession) -> None:
        self._session = session

    def add_dataset(self, dataset: TrainingDataset) -> None:
        self._session.add(TrainingDatasetRow.from_domain(dataset))
        self._session.flush()

    def dataset_by_id(self, dataset_id: UUID) -> TrainingDataset | None:
        row = self._session.get(TrainingDatasetRow, dataset_id)
        return row.to_domain() if row is not None else None

    def page_datasets(self, *, page: int, page_size: int) -> tuple[Sequence[TrainingDataset], int]:
        total = int(self._session.scalar(select(func.count()).select_from(TrainingDatasetRow)) or 0)
        rows = self._session.scalars(
            select(TrainingDatasetRow)
            .order_by(TrainingDatasetRow.updated_at.desc(), TrainingDatasetRow.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [row.to_domain() for row in rows], total

    def add_member(self, member: DatasetMember) -> None:
        self._session.add(DatasetMemberRow.from_domain(member))
        self._session.flush()

    def member_by_id(self, member_id: UUID) -> DatasetMember | None:
        row = self._session.get(DatasetMemberRow, member_id)
        return row.to_domain() if row is not None else None

    def lock_annotation_member(self, member_id: UUID) -> DatasetMember | None:
        row = self._session.scalars(
            select(DatasetMemberRow)
            .where(DatasetMemberRow.id == member_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        return row.to_domain() if row is not None else None

    def page_members(
        self, *, dataset_id: UUID, page: int, page_size: int
    ) -> tuple[Sequence[DatasetMember], int]:
        count = (
            select(func.count())
            .select_from(DatasetMemberRow)
            .where(DatasetMemberRow.dataset_id == dataset_id)
        )
        total = int(self._session.scalar(count) or 0)
        rows = self._session.scalars(
            select(DatasetMemberRow)
            .where(DatasetMemberRow.dataset_id == dataset_id)
            .order_by(DatasetMemberRow.created_at.asc(), DatasetMemberRow.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [row.to_domain() for row in rows], total

    def add_attempt(self, attempt: UploadAttempt) -> None:
        self._session.add(UploadAttemptRow.from_domain(attempt))
        self._session.flush()

    def attempt_by_id(self, attempt_id: UUID) -> UploadAttempt | None:
        row = self._session.get(UploadAttemptRow, attempt_id)
        return row.to_domain() if row is not None else None

    def attempt_by_idempotency(
        self, *, dataset_id: UUID, idempotency_key: str
    ) -> UploadAttempt | None:
        # 串行化同一数据集的检查并创建窗口。唯一约束是最后一道防线，父行锁让并发请求等待后
        # 重新读取并复用赢家，而不是在成员已经创建后向调用方暴露 IntegrityError。
        self._session.execute(
            select(TrainingDatasetRow.id)
            .where(TrainingDatasetRow.id == dataset_id)
            .with_for_update()
        ).scalar_one_or_none()
        row = self._session.scalar(
            select(UploadAttemptRow).where(
                UploadAttemptRow.dataset_id == dataset_id,
                UploadAttemptRow.idempotency_key == idempotency_key,
            )
        )
        return row.to_domain() if row is not None else None

    def save_member(
        self,
        member: DatasetMember,
        *,
        expected_attempt_id: UUID,
        expected_updated_at: datetime | None = None,
    ) -> bool:
        conditions = [
            DatasetMemberRow.id == member.id,
            DatasetMemberRow.current_attempt_id == expected_attempt_id,
        ]
        if expected_updated_at is not None:
            conditions.append(DatasetMemberRow.updated_at == expected_updated_at)
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(DatasetMemberRow).where(*conditions).values(**_member_values(member))
            ),
        )
        return result.rowcount == 1

    def save_attempt(self, attempt: UploadAttempt) -> None:
        self._session.execute(
            update(UploadAttemptRow)
            .where(UploadAttemptRow.id == attempt.id)
            .values(
                idempotency_key=attempt.idempotency_key,
                object_key=attempt.object_key,
                declared_size=attempt.declared_size,
                declared_sha256=attempt.declared_sha256,
                expires_at=attempt.expires_at,
                status=attempt.status,
                validation_job_id=attempt.validation_job_id,
                object_version_id=attempt.object_version_id,
            )
        )

    def add_action_list(self, value: ActionListRevision) -> None:
        self._session.add(ActionListRevisionRow.from_domain(value))
        self._session.flush()

    def latest_action_list(self, dataset_id: UUID) -> ActionListRevision | None:
        row = self._session.scalars(
            select(ActionListRevisionRow)
            .where(ActionListRevisionRow.dataset_id == dataset_id)
            .order_by(ActionListRevisionRow.revision.desc())
            .limit(1)
        ).first()
        return row.to_domain() if row is not None else None

    def action_list_by_revision(
        self, *, dataset_id: UUID, revision: int
    ) -> ActionListRevision | None:
        row = self._session.get(ActionListRevisionRow, (dataset_id, revision))
        return row.to_domain() if row is not None else None

    def add_annotation_context(self, value: AnnotationContext) -> None:
        self._session.add(AnnotationContextRow.from_domain(value))
        self._session.flush()

    def annotation_context_by_id(self, context_id: UUID) -> AnnotationContext | None:
        row = self._session.get(AnnotationContextRow, context_id)
        return row.to_domain() if row is not None else None

    def save_annotation_context(self, value: AnnotationContext) -> None:
        self._session.execute(
            update(AnnotationContextRow)
            .where(AnnotationContextRow.id == value.id)
            .values(
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
        )

    def annotation_submission_by_idempotency(
        self, *, dataset_id: UUID, idempotency_key: str
    ) -> AnnotationSubmission | None:
        row = self._session.scalar(
            select(AnnotationSubmissionRow).where(
                AnnotationSubmissionRow.dataset_id == dataset_id,
                AnnotationSubmissionRow.idempotency_key == idempotency_key,
            )
        )
        return row.to_domain() if row is not None else None

    def annotation_submission_by_id(self, submission_id: UUID) -> AnnotationSubmission | None:
        row = self._session.get(AnnotationSubmissionRow, submission_id)
        return row.to_domain() if row is not None else None

    def list_annotation_submissions(
        self, *, dataset_id: UUID, member_id: UUID
    ) -> Sequence[AnnotationSubmission]:
        rows = self._session.scalars(
            select(AnnotationSubmissionRow)
            .where(
                AnnotationSubmissionRow.dataset_id == dataset_id,
                AnnotationSubmissionRow.member_id == member_id,
            )
            .order_by(AnnotationSubmissionRow.created_at.asc(), AnnotationSubmissionRow.id.asc())
        ).all()
        return [row.to_domain() for row in rows]

    def add_annotation_submission(self, value: AnnotationSubmission) -> None:
        self._session.add(AnnotationSubmissionRow.from_domain(value))
        self._session.flush()

    def annotation_execution_by_id(self, execution_id: UUID) -> AnnotationExecution | None:
        row = self._session.get(AnnotationExecutionRow, execution_id)
        return row.to_domain() if row is not None else None

    def latest_annotation_execution(self, submission_id: UUID) -> AnnotationExecution | None:
        row = self._session.scalars(
            select(AnnotationExecutionRow)
            .where(AnnotationExecutionRow.submission_id == submission_id)
            .order_by(AnnotationExecutionRow.generation.desc())
            .limit(1)
        ).first()
        return row.to_domain() if row is not None else None

    def list_annotation_executions(self, submission_id: UUID) -> Sequence[AnnotationExecution]:
        rows = self._session.scalars(
            select(AnnotationExecutionRow)
            .where(AnnotationExecutionRow.submission_id == submission_id)
            .order_by(AnnotationExecutionRow.generation.asc())
        ).all()
        return [row.to_domain() for row in rows]

    def add_annotation_execution(self, value: AnnotationExecution) -> None:
        self._session.add(AnnotationExecutionRow.from_domain(value))
        self._session.flush()

    def save_annotation_execution(
        self, value: AnnotationExecution, *, expected_updated_at: datetime
    ) -> bool:
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(AnnotationExecutionRow)
                .where(
                    AnnotationExecutionRow.id == value.id,
                    AnnotationExecutionRow.updated_at == expected_updated_at,
                )
                .values(
                    job_id=value.job_id,
                    status=value.status,
                    clips=list(value.clips),
                    failure_code=value.failure_code,
                    failure_detail=value.failure_detail,
                    updated_at=value.updated_at,
                    upstream_data_id=value.upstream_data_id,
                    upstream_video_id=value.upstream_video_id,
                    derived_video_size=value.derived_video_size,
                    derived_video_sha256=value.derived_video_sha256,
                    derived_video_duration_seconds=value.derived_video_duration_seconds,
                )
            ),
        )
        return result.rowcount == 1

    def annotation_context_preparation_exists(self, member_id: UUID, context_id: UUID) -> bool:
        return (
            self._session.scalar(
                select(AnnotationContextRow.id).where(
                    AnnotationContextRow.id == context_id,
                    AnnotationContextRow.member_id == member_id,
                )
            )
            is not None
        )

    def annotation_execution_exists(self, member_id: UUID, execution_id: UUID) -> bool:
        return (
            self._session.scalar(
                select(AnnotationSubmissionRow.id)
                .join(
                    AnnotationExecutionRow,
                    AnnotationExecutionRow.submission_id == AnnotationSubmissionRow.id,
                )
                .where(
                    AnnotationExecutionRow.id == execution_id,
                    AnnotationSubmissionRow.member_id == member_id,
                )
            )
            is not None
        )


def _member_values(member: DatasetMember) -> dict[str, object]:
    return {
        "dataset_id": member.dataset_id,
        "original_filename": member.original_filename,
        "source": member.source,
        "declared_size": member.declared_size,
        "declared_sha256": member.declared_sha256,
        "current_attempt_id": member.current_attempt_id,
        "status": member.status,
        "actual_size": member.actual_size,
        "actual_sha256": member.actual_sha256,
        "duration_seconds": member.duration_seconds,
        "codec": member.codec,
        "container": member.container,
        "object_key": member.object_key,
        "object_version_id": member.object_version_id,
        "validation_job_id": member.validation_job_id,
        "failure_code": member.failure_code,
        "failure_detail": member.failure_detail,
        "recovery_action": member.recovery_action,
        "created_by": member.created_by,
        "updated_by": member.updated_by,
        "created_at": member.created_at,
        "updated_at": member.updated_at,
    }
