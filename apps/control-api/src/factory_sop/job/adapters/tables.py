"""`job_application_job`：PostgreSQL 权威任务和 outbox 投递状态。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from factory_sop.job.api import ApplicationJob, JobType
from factory_sop.persistence import Table


class ApplicationJobRow(Table):
    """一个异步应用任务；资源身份使用不带业务外键的 UUID，避免模块互相读表。"""

    __tablename__ = "job_application_job"
    __table_args__ = (UniqueConstraint("job_type", "attempt_id"),)

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    member_id: Mapped[UUID] = mapped_column(Uuid(), index=True)
    attempt_id: Mapped[UUID] = mapped_column(Uuid(), index=True)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    outbox_status: Mapped[str] = mapped_column(String(32))
    dispatch_attempts: Mapped[int] = mapped_column(Integer())
    last_dispatch_error: Mapped[str | None] = mapped_column(String(1024))

    def to_domain(self) -> ApplicationJob:
        return ApplicationJob(
            id=self.id,
            job_type=JobType(self.job_type),
            status=self.status,
            member_id=self.member_id,
            attempt_id=self.attempt_id,
            created_at=self.created_at,
            updated_at=self.updated_at,
            failure_code=self.failure_code,
        )

    @classmethod
    def from_domain(cls, job: ApplicationJob) -> ApplicationJobRow:
        return cls(
            id=job.id,
            job_type=job.job_type,
            status=job.status,
            member_id=job.member_id,
            attempt_id=job.attempt_id,
            failure_code=job.failure_code,
            created_at=job.created_at,
            updated_at=job.updated_at,
            outbox_status="pending",
            dispatch_attempts=0,
            last_dispatch_error=None,
        )
