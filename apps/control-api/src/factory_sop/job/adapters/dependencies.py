"""`job` 的 HTTP/worker 依赖装配。"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request

from factory_sop.dataset.api import DatasetResourceLookup
from factory_sop.job.adapters.dispatcher import ArqJobDispatcher
from factory_sop.job.adapters.repository import (
    PostgresAnnotationJobQueue,
    PostgresJobRepository,
    PostgresValidationJobQueue,
)
from factory_sop.job.api import AnnotationJobQueue, JobDispatcher, JobRepository, ValidationJobQueue
from factory_sop.persistence import RequestSession


def dataset_resource() -> DatasetResourceLookup:
    """由组合根接入 `dataset` 的资源归属查询契约。"""
    raise RuntimeError("job dataset resource dependency was not wired")


def job_repository(session: RequestSession) -> JobRepository:
    """请求事务中的持久化应用任务。"""
    return PostgresJobRepository(session)


def validation_jobs(
    session: RequestSession,
    job_dispatcher: Annotated[JobDispatcher, Depends(dispatcher)],
) -> ValidationJobQueue:
    """请求事务中的 dataset 校验任务创建 seam。"""
    return PostgresValidationJobQueue(session, job_dispatcher.dispatch)


def annotation_jobs(
    session: RequestSession,
    job_dispatcher: Annotated[JobDispatcher, Depends(dispatcher)],
) -> AnnotationJobQueue:
    """请求事务中的 dataset 标注任务创建 seam。"""
    return PostgresAnnotationJobQueue(session, job_dispatcher.dispatch)


def dispatcher(request: Request) -> JobDispatcher:
    """提供提交后的 Redis/ARQ 投递器。"""
    configured = getattr(request.app.state, "job_dispatcher", None)
    if configured is not None:
        return cast(JobDispatcher, configured)
    return ArqJobDispatcher.from_settings(request.app.state.settings)
