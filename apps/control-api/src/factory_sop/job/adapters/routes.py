"""异步任务查询 HTTP adapter。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_serializer

from factory_sop.auth.api import Authorized, Permission, needs_any
from factory_sop.dataset.api import DatasetResourceLookup
from factory_sop.job.adapters.dependencies import dataset_resource, job_repository
from factory_sop.job.api import ApplicationJob, JobRepository
from factory_sop.job.usecases import read_job
from factory_sop.problem import problem_openapi_response

router = APIRouter(prefix="/jobs", tags=["job"])
ProblemResponses: dict[int | str, dict[str, Any]] = {
    401: problem_openapi_response("需要认证或会话无效"),
    403: problem_openapi_response("权限不足或 CSRF 校验失败"),
    404: problem_openapi_response("异步任务不存在"),
    422: problem_openapi_response("任务编号无效"),
}


class JobView(BaseModel):
    """任务状态；未知状态保留原始字符串。"""

    id: UUID
    job_type: str
    status: str
    dataset_id: UUID | None = None
    member_id: UUID
    attempt_id: UUID
    failure_code: str | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def serialize_datetime(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _view(job: ApplicationJob) -> JobView:
    return JobView(
        id=job.id,
        job_type=job.job_type.value,
        status=job.status,
        dataset_id=job.dataset_id,
        member_id=job.member_id,
        attempt_id=job.attempt_id,
        failure_code=job.failure_code,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@router.get(
    "/{job_id}",
    response_model=JobView,
    operation_id="readJob",
    openapi_extra=needs_any(Permission.DATASET_VIEW, Permission.DATASET_IMPORT),
    responses=ProblemResponses,
)
def read_a_job(
    job_id: UUID,
    caller: Authorized,
    jobs: Annotated[JobRepository, Depends(job_repository)],
    resources: Annotated[DatasetResourceLookup, Depends(dataset_resource)],
) -> JobView:
    """读取不含上传凭据的任务状态。"""
    result = read_job(
        job_id=job_id,
        caller=caller,
        jobs=jobs,
        resource_exists=resources.resource_exists,
        dataset_resource_exists=resources.dataset_resource_exists,
    )
    return _view(result)
