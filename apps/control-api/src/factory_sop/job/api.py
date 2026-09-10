"""`job` 对 `dataset` 和 HTTP 适配器暴露的最小异步任务契约。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class JobType(StrEnum):
    """可持久化应用任务的类型。"""

    DATASET_VALIDATION = "dataset_validation"


class JobStatus(StrEnum):
    """任务在 PostgreSQL 中可观察的生命周期。"""

    PENDING = "pending"
    ENQUEUED = "enqueued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class ApplicationJob:
    """一次持久化的训练视频校验任务；不携带上传凭据。"""

    id: UUID
    job_type: JobType
    status: str
    member_id: UUID
    attempt_id: UUID
    created_at: datetime
    updated_at: datetime
    failure_code: str | None


class ValidationJobQueue(Protocol):
    """`dataset` 创建校验任务时使用的幂等接口。"""

    def get_or_create_validation(
        self, *, member_id: UUID, attempt_id: UUID, now: datetime
    ) -> ApplicationJob:
        """为同一视频尝试返回已有任务或创建一条新任务；不提交事务。"""
        ...


class JobRepository(Protocol):
    """任务状态与 outbox 的持久化 seam。"""

    def by_id(self, job_id: UUID) -> ApplicationJob | None:
        """按公开任务身份读取任务。"""
        ...

    def by_attempt(self, attempt_id: UUID) -> ApplicationJob | None:
        """读取某次视频尝试的校验任务。"""
        ...

    def add(self, job: ApplicationJob) -> None:
        """保存一条任务和待投递事实；不提交事务。"""
        ...

    def recover_stale_running(self, *, now: datetime, stale_after_seconds: int) -> int:
        """把超过执行租约的运行中任务恢复为待投递，并返回恢复数量。"""
        ...

    def mark_running(self, *, job_id: UUID, now: datetime) -> ApplicationJob | None:
        """以条件更新领取任务；重复投递只允许一个执行者前进。"""
        ...

    def mark_enqueued(self, *, job_id: UUID, now: datetime) -> None:
        """记录 outbox 已投递，不改变业务资源结果。"""
        ...

    def record_dispatch_failure(self, *, job_id: UUID, error: str, now: datetime) -> None:
        """保留投递失败，供后续 outbox 扫描再次投递。"""
        ...

    def finish(
        self,
        *,
        job_id: UUID,
        status: str,
        failure_code: str | None,
        now: datetime,
        expected_updated_at: datetime,
    ) -> bool:
        """按领取租约结案，返回是否仍由本次 worker 持有；不提交事务。"""
        ...

    def pending(self, *, limit: int) -> list[ApplicationJob]:
        """返回仍需投递的任务，供补投扫描使用。"""
        ...


class JobDispatcher(Protocol):
    """提交事务后把任务 id 投递给 Redis/ARQ 的适配器 seam。"""

    def dispatch(self, job_id: UUID) -> None:
        """投递一个已提交的任务；失败由 outbox 扫描恢复。"""
        ...
