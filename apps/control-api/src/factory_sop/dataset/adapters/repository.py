"""`dataset` 的 PostgreSQL 仓储适配器。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.dataset.adapters.tables import (
    ActionListRevisionRow,
    AnnotationContextRow,
    AnnotationExecutionRow,
    AnnotationSubmissionRow,
    DatasetArtifactRow,
    DatasetMemberRow,
    TrainingDatasetRow,
    UploadAttemptRow,
    UsageCheckRow,
    VlmCandidateRow,
)
from factory_sop.dataset.model import (
    ActionListRevision,
    AnnotationContext,
    AnnotationExecution,
    AnnotationSubmission,
    AttemptStatus,
    DatasetArtifact,
    DatasetMember,
    TrainingDataset,
    UploadAttempt,
    UsageCheck,
    UsageKind,
    VlmCandidate,
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

    def list_members(self, *, dataset_id: UUID) -> Sequence[DatasetMember]:
        rows = self._session.scalars(
            select(DatasetMemberRow)
            .where(DatasetMemberRow.dataset_id == dataset_id)
            .order_by(DatasetMemberRow.id)
        ).all()
        return [row.to_domain() for row in rows]

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

    def expired_pending_attempts(self, *, now: datetime, limit: int) -> Sequence[UploadAttempt]:
        rows = self._session.scalars(
            select(UploadAttemptRow)
            .where(
                UploadAttemptRow.status == AttemptStatus.PENDING_UPLOAD.value,
                UploadAttemptRow.expires_at <= now,
            )
            .order_by(UploadAttemptRow.expires_at, UploadAttemptRow.id)
            .limit(limit)
        ).all()
        return [row.to_domain() for row in rows]

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

    def latest_annotation_submission(self, *, member_id: UUID) -> AnnotationSubmission | None:
        row = self._session.scalars(
            select(AnnotationSubmissionRow)
            .where(AnnotationSubmissionRow.member_id == member_id)
            .order_by(AnnotationSubmissionRow.revision.desc())
            .limit(1)
        ).first()
        return row.to_domain() if row is not None else None

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

    def add_vlm_candidate(self, value: VlmCandidate) -> None:
        try:
            with self._session.begin_nested():
                self._session.add(VlmCandidateRow.from_domain(value))
                self._session.flush()
        except IntegrityError:
            if (
                self._session.scalar(
                    select(VlmCandidateRow.id).where(
                        VlmCandidateRow.dataset_id == value.dataset_id,
                        VlmCandidateRow.revision == value.revision,
                    )
                )
                is None
            ):
                raise

    def vlm_candidate_by_id(self, candidate_id: UUID) -> VlmCandidate | None:
        row = self._session.get(VlmCandidateRow, candidate_id)
        return row.to_domain() if row is not None else None

    def latest_vlm_candidate(self, dataset_id: UUID) -> VlmCandidate | None:
        row = self._session.scalars(
            select(VlmCandidateRow)
            .where(VlmCandidateRow.dataset_id == dataset_id)
            .order_by(VlmCandidateRow.revision.desc())
            .limit(1)
        ).first()
        return row.to_domain() if row is not None else None

    def list_vlm_candidates(self, dataset_id: UUID) -> Sequence[VlmCandidate]:
        rows = self._session.scalars(
            select(VlmCandidateRow)
            .where(VlmCandidateRow.dataset_id == dataset_id)
            .order_by(VlmCandidateRow.revision.asc())
        ).all()
        return [row.to_domain() for row in rows]

    def add_usage_check(self, value: UsageCheck) -> None:
        self._session.add(UsageCheckRow.from_domain(value))
        self._session.flush()

    def usage_check_by_id(self, check_id: UUID) -> UsageCheck | None:
        row = self._session.get(UsageCheckRow, check_id)
        return row.to_domain() if row is not None else None

    def latest_usage_check(self, *, dataset_id: UUID, kind: UsageKind) -> UsageCheck | None:
        row = self._session.scalars(
            select(UsageCheckRow)
            .where(UsageCheckRow.dataset_id == dataset_id, UsageCheckRow.kind == kind.value)
            .order_by(UsageCheckRow.created_at.desc(), UsageCheckRow.id.desc())
            .limit(1)
        ).first()
        return row.to_domain() if row is not None else None

    def list_usage_checks(self, dataset_id: UUID) -> Sequence[UsageCheck]:
        rows = self._session.scalars(
            select(UsageCheckRow)
            .where(UsageCheckRow.dataset_id == dataset_id)
            .order_by(UsageCheckRow.created_at.desc(), UsageCheckRow.id.desc())
        ).all()
        return [row.to_domain() for row in rows]

    def save_usage_check(self, value: UsageCheck, *, expected_updated_at: datetime) -> bool:
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(UsageCheckRow)
                .where(
                    UsageCheckRow.id == value.id,
                    UsageCheckRow.updated_at == expected_updated_at,
                )
                .values(
                    status=value.status.value,
                    input_digest=value.input_digest,
                    input_snapshot=dict(value.input_snapshot),
                    summary=dict(value.summary),
                    issues=[dict(item) for item in value.issues],
                    base_commit=value.base_commit,
                    contract_version=value.contract_version,
                    candidate_id=value.candidate_id,
                    job_id=value.job_id,
                    updated_at=value.updated_at,
                )
            ),
        )
        return result.rowcount == 1

    def add_artifact(self, value: DatasetArtifact) -> None:
        try:
            with self._session.begin_nested():
                self._session.add(DatasetArtifactRow.from_domain(value))
                self._session.flush()
        except IntegrityError:
            if (
                self.artifact_by_input(
                    dataset_id=value.dataset_id,
                    input_digest=value.input_digest,
                )
                is None
            ):
                raise

    def artifact_by_id(self, artifact_id: UUID) -> DatasetArtifact | None:
        row = self._session.get(DatasetArtifactRow, artifact_id)
        return row.to_domain() if row is not None else None

    def artifact_by_input(self, *, dataset_id: UUID, input_digest: str) -> DatasetArtifact | None:
        row = self._session.scalar(
            select(DatasetArtifactRow).where(
                DatasetArtifactRow.dataset_id == dataset_id,
                DatasetArtifactRow.input_digest == input_digest,
            )
        )
        return row.to_domain() if row is not None else None

    def list_artifacts(self, dataset_id: UUID) -> Sequence[DatasetArtifact]:
        rows = self._session.scalars(
            select(DatasetArtifactRow)
            .where(DatasetArtifactRow.dataset_id == dataset_id)
            .order_by(DatasetArtifactRow.created_at.desc(), DatasetArtifactRow.id.desc())
        ).all()
        return [row.to_domain() for row in rows]

    def list_artifact_cleanup_candidates(self, *, limit: int) -> Sequence[DatasetArtifact]:
        rows = self._session.scalars(
            select(DatasetArtifactRow).order_by(
                DatasetArtifactRow.updated_at.asc(), DatasetArtifactRow.id.asc()
            )
        ).all()
        values = []
        for row in rows:
            raw_keys = (
                row.manifest.get("orphan_candidate_keys")
                if isinstance(row.manifest, dict)
                else None
            )
            if isinstance(raw_keys, list) and any(isinstance(key, str) and key for key in raw_keys):
                values.append(row.to_domain())
        return values[:limit]

    def save_artifact(self, value: DatasetArtifact, *, expected_updated_at: datetime) -> bool:
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(DatasetArtifactRow)
                .where(
                    DatasetArtifactRow.id == value.id,
                    DatasetArtifactRow.updated_at == expected_updated_at,
                )
                .values(
                    status=value.status.value,
                    object_key=value.object_key,
                    artifact_sha256=value.artifact_sha256,
                    artifact_size=value.artifact_size,
                    manifest=dict(value.manifest),
                    failure_code=value.failure_code,
                    failure_detail=value.failure_detail,
                    retryable=value.retryable,
                    recovery_action=value.recovery_action,
                    job_id=value.job_id,
                    updated_at=value.updated_at,
                )
            ),
        )
        return result.rowcount == 1


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
