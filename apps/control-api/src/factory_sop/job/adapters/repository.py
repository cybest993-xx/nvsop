"""`job` 的 PostgreSQL 仓储和校验任务幂等队列。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, case, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.identifiers import new_id
from factory_sop.job.adapters.tables import ApplicationJobRow
from factory_sop.job.api import ApplicationJob, JobStatus, JobType
from factory_sop.persistence import register_after_commit


class PostgresJobRepository:
    """通过请求/worker 事务访问 `job_application_job`，不自行提交。"""

    def __init__(
        self,
        session: DatabaseSession,
        *,
        dispatch: Callable[[UUID], None] | None = None,
    ) -> None:
        self._session = session
        self._dispatch = dispatch
        self._dispatch_ids: set[UUID] = set()

    def by_id(self, job_id: UUID) -> ApplicationJob | None:
        row = self._session.get(ApplicationJobRow, job_id)
        return row.to_domain() if row is not None else None

    def by_attempt(self, attempt_id: UUID) -> ApplicationJob | None:
        row = self._session.scalar(
            select(ApplicationJobRow).where(ApplicationJobRow.attempt_id == attempt_id)
        )
        return row.to_domain() if row is not None else None

    def _by_attempt_type(self, *, attempt_id: UUID, job_type: JobType) -> ApplicationJob | None:
        row = self._session.scalar(
            select(ApplicationJobRow).where(
                ApplicationJobRow.attempt_id == attempt_id,
                ApplicationJobRow.job_type == job_type.value,
            )
        )
        return row.to_domain() if row is not None else None

    def add(self, job: ApplicationJob) -> None:
        self._session.add(ApplicationJobRow.from_domain(job))
        self._session.flush()

    def get_or_create_validation(
        self, *, member_id: UUID, attempt_id: UUID, now: datetime
    ) -> ApplicationJob:
        existing = self.by_attempt(attempt_id)
        if existing is not None:
            if existing.status == JobStatus.FAILED:
                self._session.execute(
                    update(ApplicationJobRow)
                    .where(ApplicationJobRow.id == existing.id)
                    .values(
                        status=JobStatus.PENDING,
                        failure_code=None,
                        updated_at=now,
                        outbox_status="pending",
                    )
                )
                existing = replace(
                    existing,
                    status=JobStatus.PENDING,
                    updated_at=now,
                    failure_code=None,
                )
            self._remember_for_dispatch(existing.id)
            return existing
        candidate = ApplicationJob(
            id=new_id(),
            job_type=JobType.DATASET_VALIDATION,
            status=JobStatus.PENDING,
            member_id=member_id,
            attempt_id=attempt_id,
            created_at=now,
            updated_at=now,
            failure_code=None,
        )
        try:
            # 保存点只隔离唯一键竞争；如果回滚整个请求，会抹掉成员已经持久化的待校验状态。
            with self._session.begin_nested():
                self.add(candidate)
        except IntegrityError:
            existing = self.by_attempt(attempt_id)
            if existing is None:
                raise
            self._remember_for_dispatch(existing.id)
            return existing
        self._remember_for_dispatch(candidate.id)
        return candidate

    def get_or_create_annotation(
        self, *, member_id: UUID, attempt_id: UUID, now: datetime
    ) -> ApplicationJob:
        """为一个标注执行代次创建幂等任务。"""
        existing = self._by_attempt_type(
            attempt_id=attempt_id,
            job_type=JobType.DATASET_ANNOTATION,
        )
        if existing is not None:
            # 标注显式 retry 会创建新代次；幂等重放不能把已失败的同一代次伪装成待执行。
            self._remember_for_dispatch(existing.id)
            return existing
        candidate = ApplicationJob(
            id=new_id(),
            job_type=JobType.DATASET_ANNOTATION,
            status=JobStatus.PENDING,
            member_id=member_id,
            attempt_id=attempt_id,
            created_at=now,
            updated_at=now,
            failure_code=None,
        )
        try:
            with self._session.begin_nested():
                self.add(candidate)
        except IntegrityError:
            existing = self._by_attempt_type(
                attempt_id=attempt_id,
                job_type=JobType.DATASET_ANNOTATION,
            )
            if existing is None:
                raise
            self._remember_for_dispatch(existing.id)
            return existing
        self._remember_for_dispatch(candidate.id)
        return candidate

    def get_or_create_annotation_preparation(
        self, *, member_id: UUID, attempt_id: UUID, now: datetime
    ) -> ApplicationJob:
        """为标注上下文准备基座副本创建幂等任务。"""
        job_type = JobType.DATASET_ANNOTATION_PREPARATION
        existing = self._by_attempt_type(attempt_id=attempt_id, job_type=job_type)
        if existing is not None:
            if existing.status == JobStatus.FAILED:
                self._session.execute(
                    update(ApplicationJobRow)
                    .where(ApplicationJobRow.id == existing.id)
                    .values(
                        status=JobStatus.PENDING,
                        failure_code=None,
                        updated_at=now,
                        outbox_status="pending",
                    )
                )
                existing = replace(
                    existing,
                    status=JobStatus.PENDING,
                    updated_at=now,
                    failure_code=None,
                )
            self._remember_for_dispatch(existing.id)
            return existing
        candidate = ApplicationJob(
            id=new_id(),
            job_type=job_type,
            status=JobStatus.PENDING,
            member_id=member_id,
            attempt_id=attempt_id,
            created_at=now,
            updated_at=now,
            failure_code=None,
        )
        try:
            with self._session.begin_nested():
                self.add(candidate)
        except IntegrityError:
            existing = self._by_attempt_type(attempt_id=attempt_id, job_type=job_type)
            if existing is None:
                raise
            self._remember_for_dispatch(existing.id)
            return existing
        self._remember_for_dispatch(candidate.id)
        return candidate

    def _remember_for_dispatch(self, job_id: UUID) -> None:
        """把需要提交后投递的任务登记到通用提交后动作中。"""
        dispatch = self._dispatch
        if dispatch is None or job_id in self._dispatch_ids:
            return
        self._dispatch_ids.add(job_id)
        register_after_commit(self._session, lambda: dispatch(job_id))

    def recover_stale_running(self, *, now: datetime, stale_after_seconds: int) -> int:
        """恢复过期执行租约，并保留已有的投递诊断。"""
        cutoff = now - timedelta(seconds=stale_after_seconds)
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(ApplicationJobRow)
                .where(
                    ApplicationJobRow.status == JobStatus.RUNNING.value,
                    ApplicationJobRow.updated_at <= cutoff,
                )
                .values(
                    status=JobStatus.PENDING.value,
                    outbox_status="pending",
                    updated_at=now,
                    last_dispatch_error=case(
                        (
                            ApplicationJobRow.last_dispatch_error.is_(None),
                            f"worker lease expired after {stale_after_seconds} seconds",
                        ),
                        else_=ApplicationJobRow.last_dispatch_error,
                    ),
                )
            ),
        )
        return result.rowcount

    def mark_running(self, *, job_id: UUID, now: datetime) -> ApplicationJob | None:
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(ApplicationJobRow)
                .where(
                    ApplicationJobRow.id == job_id,
                    ApplicationJobRow.status.in_(
                        (JobStatus.PENDING.value, JobStatus.ENQUEUED.value)
                    ),
                )
                .values(status=JobStatus.RUNNING, updated_at=now)
            ),
        )
        if result.rowcount != 1:
            return None
        return self.by_id(job_id)

    def mark_enqueued(self, *, job_id: UUID, now: datetime) -> None:
        self._session.execute(
            update(ApplicationJobRow)
            .where(ApplicationJobRow.id == job_id)
            .values(
                status=case(
                    (ApplicationJobRow.status == JobStatus.PENDING.value, JobStatus.ENQUEUED.value),
                    else_=ApplicationJobRow.status,
                ),
                outbox_status="dispatched",
                dispatch_attempts=ApplicationJobRow.dispatch_attempts + 1,
                last_dispatch_error=None,
                updated_at=now,
            )
        )

    def record_dispatch_failure(self, *, job_id: UUID, error: str, now: datetime) -> None:
        self._session.execute(
            update(ApplicationJobRow)
            .where(ApplicationJobRow.id == job_id)
            .values(
                outbox_status="pending",
                dispatch_attempts=ApplicationJobRow.dispatch_attempts + 1,
                last_dispatch_error=error[:1024],
                updated_at=now,
            )
        )

    def finish(
        self,
        *,
        job_id: UUID,
        status: str,
        failure_code: str | None,
        now: datetime,
        expected_updated_at: datetime,
    ) -> bool:
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(ApplicationJobRow)
                .where(
                    ApplicationJobRow.id == job_id,
                    ApplicationJobRow.status == JobStatus.RUNNING.value,
                    ApplicationJobRow.updated_at == expected_updated_at,
                )
                .values(status=status, failure_code=failure_code, updated_at=now)
            ),
        )
        return result.rowcount == 1

    def pending(self, *, limit: int) -> list[ApplicationJob]:
        rows = self._session.scalars(
            select(ApplicationJobRow)
            .where(ApplicationJobRow.outbox_status == "pending")
            .where(
                ApplicationJobRow.status.not_in(
                    (JobStatus.SUCCEEDED.value, JobStatus.FAILED.value, JobStatus.SUPERSEDED.value)
                )
            )
            .order_by(ApplicationJobRow.created_at, ApplicationJobRow.id)
            .limit(limit)
        ).all()
        return [row.to_domain() for row in rows]


class PostgresAnnotationJobQueue:
    """`dataset` 使用的标注任务创建 seam。"""

    def __init__(
        self,
        session: DatabaseSession,
        dispatch: Callable[[UUID], None] | None = None,
    ) -> None:
        self._repository = PostgresJobRepository(session, dispatch=dispatch)

    def get_or_create_annotation(
        self, *, member_id: UUID, attempt_id: UUID, now: datetime
    ) -> ApplicationJob:
        return self._repository.get_or_create_annotation(
            member_id=member_id,
            attempt_id=attempt_id,
            now=now,
        )

    def get_or_create_annotation_preparation(
        self, *, member_id: UUID, attempt_id: UUID, now: datetime
    ) -> ApplicationJob:
        return self._repository.get_or_create_annotation_preparation(
            member_id=member_id,
            attempt_id=attempt_id,
            now=now,
        )


class PostgresValidationJobQueue:
    """`dataset` 使用的校验任务创建 seam。"""

    def __init__(
        self,
        session: DatabaseSession,
        dispatch: Callable[[UUID], None] | None = None,
    ) -> None:
        self._repository = PostgresJobRepository(session, dispatch=dispatch)

    def get_or_create_validation(
        self, *, member_id: UUID, attempt_id: UUID, now: datetime
    ) -> ApplicationJob:
        return self._repository.get_or_create_validation(
            member_id=member_id,
            attempt_id=attempt_id,
            now=now,
        )
