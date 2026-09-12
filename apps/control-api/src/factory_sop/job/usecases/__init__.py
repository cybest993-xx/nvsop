"""异步任务的读取用例；权限位于用例边界而非 HTTP 路由。"""

from __future__ import annotations

from collections.abc import Callable
from typing import assert_never
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.job.api import ApplicationJob, JobRepository, JobType
from factory_sop.job.errors import JobRefusalCode, JobRefusedError


def read_job(
    *,
    job_id: UUID,
    caller: Caller,
    jobs: JobRepository,
    resource_exists: Callable[[UUID, UUID], bool],
    dataset_resource_exists: Callable[[UUID, UUID], bool],
) -> ApplicationJob:
    """读取一个任务，并确认任务资源属于可查询的数据集视频。"""
    if not (caller.holds(Permission.DATASET_VIEW) or caller.holds(Permission.DATASET_IMPORT)):
        authorize(caller, Permission.DATASET_VIEW)
    job = jobs.by_id(job_id)
    if job is None:
        raise JobRefusedError(JobRefusalCode.JOB_NOT_FOUND, "异步任务不存在")
    match job.job_type:
        case JobType.DATASET_VALIDATION:
            pass
        case (
            JobType.DATASET_ANNOTATION
            | JobType.DATASET_ANNOTATION_PREPARATION
            | JobType.DATASET_USAGE_CHECK
            | JobType.DATASET_ARTIFACT
        ):
            authorize(caller, Permission.DATASET_VIEW)
        case _:
            assert_never(job.job_type)
    resource_found = (
        dataset_resource_exists(job.dataset_id, job.attempt_id)
        if job.dataset_id is not None
        else resource_exists(job.member_id, job.attempt_id)
    )
    if not resource_found:
        raise JobRefusedError(JobRefusalCode.JOB_RESOURCE_NOT_FOUND, "任务关联的资源不存在")
    return job
