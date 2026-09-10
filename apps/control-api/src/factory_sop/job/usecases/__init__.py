"""异步任务的读取用例；权限位于用例边界而非 HTTP 路由。"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.job.api import ApplicationJob, JobRepository
from factory_sop.job.errors import JobRefusalCode, JobRefusedError


def read_job(
    *,
    job_id: UUID,
    caller: Caller,
    jobs: JobRepository,
    resource_exists: Callable[[UUID, UUID], bool],
) -> ApplicationJob:
    """读取一个任务，并确认任务资源属于可查询的数据集视频。"""
    if not (caller.holds(Permission.DATASET_VIEW) or caller.holds(Permission.DATASET_IMPORT)):
        authorize(caller, Permission.DATASET_VIEW)
    job = jobs.by_id(job_id)
    if job is None:
        raise JobRefusedError(JobRefusalCode.JOB_NOT_FOUND, "异步任务不存在")
    if not resource_exists(job.member_id, job.attempt_id):
        raise JobRefusedError(JobRefusalCode.JOB_RESOURCE_NOT_FOUND, "任务关联的视频不存在")
    return job
