"""增强命令的受理、历史查询与派发前取消 HTTP 接口。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, status
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from factory_sop.auth.api import Authorized, Permission, needs
from factory_sop.dataset.adapters import dependencies
from factory_sop.dataset.model import AugmentationCommand, AugmentationStatus
from factory_sop.dataset.repository import AugmentationRepository
from factory_sop.dataset.usecases.augmentation import (
    cancel_augmentation,
    list_augmentations,
    read_augmentation,
    request_augmentation,
)
from factory_sop.job.api import AugmentationJobQueue
from factory_sop.problem import problem_openapi_response
from factory_sop.responses import DEFAULT_PAGE_SIZE, MAXIMUM_PAGE_SIZE, ItemPage

router = APIRouter()
_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: problem_openapi_response("需要认证或会话无效"),
    403: problem_openapi_response("权限不足或 CSRF 校验失败"),
    404: problem_openapi_response("训练数据集或增强命令不存在"),
    409: problem_openapi_response("输入、幂等键或命令状态冲突"),
    422: problem_openapi_response("请求无效"),
}


class RequestAugmentationInput(BaseModel):
    """只接受已登记标注执行身份，不接受客户端路径、URL 或基座身份。"""

    model_config = ConfigDict(extra="forbid")
    annotation_execution_id: UUID
    frame_drop_iterations: StrictInt = Field(ge=0, le=10)


class AugmentationView(BaseModel):
    """公开状态与来源摘要；内部冻结载荷、共享卷清单和对象键留在 dataset。"""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    dataset_id: UUID
    annotation_execution_id: UUID
    job_id: UUID | None
    frame_drop_iterations: int
    input_source: str
    original_filename: str
    status: AugmentationStatus
    dispatched_at: datetime | None
    base_augmentation_id: str | None
    base_status: str | None
    base_progress: float | None
    last_observed_at: datetime | None
    failure_code: str | None
    result_status: Literal["not_registered", "registered"]
    result_file_count: int
    result_total_bytes: int
    result_failure_code: str | None
    cancelled_by: UUID | None
    cancelled_at: datetime | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime


def _public_view(value: AugmentationCommand) -> AugmentationView:
    """从已登记的内部事实只投影被授权查看的非敏感字段。"""
    files = value.result_manifest.get("files", ())
    return AugmentationView(
        id=value.id,
        dataset_id=value.dataset_id,
        annotation_execution_id=value.annotation_execution_id,
        job_id=value.job_id,
        frame_drop_iterations=value.frame_drop_iterations,
        input_source=value.input_snapshot["source"],
        original_filename=value.input_snapshot["original_filename"],
        status=value.status,
        dispatched_at=value.dispatched_at,
        base_augmentation_id=value.base_augmentation_id,
        base_status=value.base_observation.get("status"),
        base_progress=value.base_observation.get("progress"),
        last_observed_at=value.last_observed_at,
        failure_code=value.failure_code,
        # 0075 中的 CHECK 约束仅允许这两个持久状态。
        result_status=cast(Literal["not_registered", "registered"], value.result_status),
        result_file_count=len(files),
        result_total_bytes=sum(item["size"] for item in files),
        result_failure_code=value.result_failure_code,
        cancelled_by=value.cancelled_by,
        cancelled_at=value.cancelled_at,
        created_by=value.created_by,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


@router.post(
    "/{dataset_id}/augmentation-commands",
    response_model=AugmentationView,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="requestDatasetAugmentation",
    openapi_extra=needs(Permission.DATASET_EDIT),
    responses=_RESPONSES,
)
def request_dataset_augmentation(
    dataset_id: UUID,
    submitted: RequestAugmentationInput,
    caller: Authorized,
    datasets: Annotated[AugmentationRepository, Depends(dependencies.datasets)],
    jobs: Annotated[AugmentationJobQueue, Depends(dependencies.usage_jobs)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> AugmentationView:
    value = request_augmentation(
        dataset_id=dataset_id,
        execution_id=submitted.annotation_execution_id,
        frame_drop_iterations=submitted.frame_drop_iterations,
        idempotency_key=idempotency_key,
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
        jobs=jobs,
    )
    return _public_view(value)


@router.get(
    "/{dataset_id}/augmentation-commands",
    response_model=ItemPage[AugmentationView],
    operation_id="listDatasetAugmentations",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=_RESPONSES,
)
def list_dataset_augmentations(
    dataset_id: UUID,
    caller: Authorized,
    datasets: Annotated[AugmentationRepository, Depends(dependencies.datasets)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ItemPage[AugmentationView]:
    values = list_augmentations(dataset_id=dataset_id, caller=caller, datasets=datasets)
    return ItemPage(
        items=[_public_view(v) for v in values[(page - 1) * page_size : page * page_size]],
        page=page,
        page_size=page_size,
        total=len(values),
    )


@router.get(
    "/{dataset_id}/augmentation-commands/{command_id}",
    response_model=AugmentationView,
    operation_id="readDatasetAugmentation",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=_RESPONSES,
)
def read_dataset_augmentation(
    dataset_id: UUID,
    command_id: UUID,
    caller: Authorized,
    datasets: Annotated[AugmentationRepository, Depends(dependencies.datasets)],
) -> AugmentationView:
    return _public_view(
        read_augmentation(
            dataset_id=dataset_id, command_id=command_id, caller=caller, datasets=datasets
        )
    )


@router.post(
    "/{dataset_id}/augmentation-commands/{command_id}/cancel",
    response_model=AugmentationView,
    operation_id="cancelPendingDatasetAugmentation",
    openapi_extra=needs(Permission.DATASET_EDIT),
    responses=_RESPONSES,
)
def cancel_pending_dataset_augmentation(
    dataset_id: UUID,
    command_id: UUID,
    caller: Authorized,
    datasets: Annotated[AugmentationRepository, Depends(dependencies.datasets)],
) -> AugmentationView:
    return _public_view(
        cancel_augmentation(
            dataset_id=dataset_id,
            command_id=command_id,
            caller=caller,
            now=datetime.now(UTC),
            datasets=datasets,
        )
    )
