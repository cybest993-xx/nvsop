"""训练数据集、视频成员和直传生命周期的 HTTP adapter。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_serializer,
    field_validator,
)

from factory_sop.auth.api import Authorized, Permission, needs
from factory_sop.dataset import model
from factory_sop.dataset.adapters import dependencies
from factory_sop.dataset.repository import DatasetRepository
from factory_sop.dataset.storage import ObjectStorage
from factory_sop.dataset.usecases import (
    confirm_video_upload,
    create_training_dataset,
    list_dataset_members,
    list_training_datasets,
    read_dataset_member,
    read_training_dataset,
    request_video_upload,
    retry_video_upload,
)
from factory_sop.job.api import ApplicationJob, ValidationJobQueue
from factory_sop.problem import problem_openapi_response
from factory_sop.responses import DEFAULT_PAGE_SIZE, MAXIMUM_PAGE_SIZE, ItemPage

router = APIRouter(prefix="/training-datasets", tags=["dataset"])
ProblemResponses: dict[int | str, dict[str, Any]] = {
    401: problem_openapi_response("需要认证或会话无效"),
    403: problem_openapi_response("权限不足或 CSRF 校验失败"),
    404: problem_openapi_response("训练数据集、视频或上传尝试不存在"),
    409: problem_openapi_response("视频当前状态不允许该操作"),
    422: problem_openapi_response("请求或视频校验无效"),
    503: problem_openapi_response("对象存储或媒体探测暂时不可用"),
}


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class CreateDatasetInput(BaseModel):
    """创建训练数据集的输入。"""

    model_config = ConfigDict(extra="forbid")

    name: StrictStr = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("训练数据集名称不能为空")
        return value.strip()


class RequestVideoUploadInput(BaseModel):
    """申请直传时提交的声明；不接受对象 URL 或客户端媒体事实。"""

    model_config = ConfigDict(extra="forbid")

    original_filename: StrictStr = Field(min_length=1, max_length=255)
    source: StrictStr = Field(min_length=1, max_length=255)
    declared_size: StrictInt = Field(gt=0)
    declared_sha256: StrictStr = Field(min_length=64, max_length=64)


class ConfirmVideoUploadInput(BaseModel):
    """确认某个上传尝试已由客户端直传完成。"""

    model_config = ConfigDict(extra="forbid")

    attempt_id: UUID


class RetryVideoUploadInput(BaseModel):
    """选择重新上传或对固定内容重新校验。"""

    model_config = ConfigDict(extra="forbid")

    mode: model.RetryMode


class DatasetView(BaseModel):
    id: UUID
    name: str
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def serialize_datetime(self, value: datetime) -> str:
        return _utc(value)


class DatasetMemberView(BaseModel):
    id: UUID
    dataset_id: UUID
    original_filename: str
    source: str
    declared_size: int
    declared_sha256: str
    current_attempt_id: UUID
    status: str
    actual_size: int | None
    actual_sha256: str | None
    duration_seconds: float | None
    codec: str | None
    container: str | None
    validation_job_id: UUID | None
    failure_code: str | None
    failure_detail: str | None
    recovery_action: str | None
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def serialize_datetime(self, value: datetime) -> str:
        return _utc(value)


class UploadAttemptView(BaseModel):
    id: UUID
    member_id: UUID
    status: str
    declared_size: int
    declared_sha256: str
    expires_at: datetime
    object_version_id: str | None

    @field_serializer("expires_at")
    def serialize_expires_at(self, value: datetime) -> str:
        return _utc(value)


class UploadInstructionsView(BaseModel):
    """仅随申请直传响应返回的短期签名说明。"""

    method: str
    url: str
    fields: dict[str, str]
    headers: dict[str, str]
    expires_at: datetime
    max_bytes: int
    object_key: str

    @field_serializer("expires_at")
    def serialize_expires_at(self, value: datetime) -> str:
        return _utc(value)


class UploadRequestView(BaseModel):
    member: DatasetMemberView
    attempt: UploadAttemptView
    upload: UploadInstructionsView | None


class JobView(BaseModel):
    id: UUID
    job_type: str
    status: str
    member_id: UUID
    attempt_id: UUID
    failure_code: str | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def serialize_datetime(self, value: datetime) -> str:
        return _utc(value)


class ConfirmationView(BaseModel):
    member: DatasetMemberView
    job: JobView | None


class RetryView(BaseModel):
    member: DatasetMemberView
    attempt: UploadAttemptView
    upload: UploadInstructionsView | None
    job: JobView | None


AcceptedConfirmationResponse: dict[str, Any] = {
    "description": "校验任务已接受",
    "model": ConfirmationView,
}
AcceptedRetryResponse: dict[str, Any] = {
    "description": "校验任务已接受",
    "model": RetryView,
}


def _dataset_view(value: model.TrainingDataset) -> DatasetView:
    return DatasetView(
        id=value.id,
        name=value.name,
        created_by=value.created_by,
        updated_by=value.updated_by,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def _member_view(value: model.DatasetMember) -> DatasetMemberView:
    return DatasetMemberView(
        id=value.id,
        dataset_id=value.dataset_id,
        original_filename=value.original_filename,
        source=value.source,
        declared_size=value.declared_size,
        declared_sha256=value.declared_sha256,
        current_attempt_id=value.current_attempt_id,
        status=value.status,
        actual_size=value.actual_size,
        actual_sha256=value.actual_sha256,
        duration_seconds=value.duration_seconds,
        codec=value.codec,
        container=value.container,
        validation_job_id=value.validation_job_id,
        failure_code=value.failure_code,
        failure_detail=value.failure_detail,
        recovery_action=value.recovery_action,
        created_by=value.created_by,
        updated_by=value.updated_by,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def _attempt_view(value: model.UploadAttempt) -> UploadAttemptView:
    return UploadAttemptView(
        id=value.id,
        member_id=value.member_id,
        status=value.status,
        declared_size=value.declared_size,
        declared_sha256=value.declared_sha256,
        expires_at=value.expires_at,
        object_version_id=value.object_version_id,
    )


def _upload_view(value: model.UploadInstructions | None) -> UploadInstructionsView | None:
    if value is None:
        return None
    return UploadInstructionsView(
        method=value.method,
        url=value.url,
        fields=value.fields,
        headers=value.headers,
        expires_at=value.expires_at,
        max_bytes=value.max_bytes,
        object_key=value.object_key,
    )


def _job_view(value: ApplicationJob | None) -> JobView | None:
    if value is None:
        return None
    return JobView(
        id=value.id,
        job_type=value.job_type.value,
        status=value.status,
        member_id=value.member_id,
        attempt_id=value.attempt_id,
        failure_code=value.failure_code,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=DatasetView,
    operation_id="createTrainingDataset",
    openapi_extra=needs(Permission.DATASET_IMPORT),
    responses=ProblemResponses,
)
def create_a_training_dataset(
    submitted: CreateDatasetInput,
    caller: Authorized,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
) -> DatasetView:
    """创建训练数据集分组。"""
    return _dataset_view(
        create_training_dataset(
            name=submitted.name,
            caller=caller,
            now=datetime.now(UTC),
            datasets=datasets,
        )
    )


@router.get(
    "",
    response_model=ItemPage[DatasetView],
    operation_id="listTrainingDatasets",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=ProblemResponses,
)
def list_the_training_datasets(
    caller: Authorized,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ItemPage[DatasetView]:
    items, total = list_training_datasets(
        caller=caller,
        page=page,
        page_size=page_size,
        datasets=datasets,
    )
    return ItemPage(
        items=[_dataset_view(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get(
    "/{dataset_id}",
    response_model=DatasetView,
    operation_id="readTrainingDataset",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=ProblemResponses,
)
def read_a_training_dataset(
    dataset_id: UUID,
    caller: Authorized,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
) -> DatasetView:
    return _dataset_view(
        read_training_dataset(dataset_id=dataset_id, caller=caller, datasets=datasets)
    )


@router.get(
    "/{dataset_id}/members",
    response_model=ItemPage[DatasetMemberView],
    operation_id="listDatasetMembers",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=ProblemResponses,
)
def list_the_dataset_members(
    dataset_id: UUID,
    caller: Authorized,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ItemPage[DatasetMemberView]:
    items, total = list_dataset_members(
        dataset_id=dataset_id,
        caller=caller,
        page=page,
        page_size=page_size,
        datasets=datasets,
    )
    return ItemPage(
        items=[_member_view(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get(
    "/{dataset_id}/members/{member_id}",
    response_model=DatasetMemberView,
    operation_id="readDatasetMember",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=ProblemResponses,
)
def read_a_dataset_member(
    dataset_id: UUID,
    member_id: UUID,
    caller: Authorized,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
) -> DatasetMemberView:
    return _member_view(
        read_dataset_member(
            dataset_id=dataset_id,
            member_id=member_id,
            caller=caller,
            datasets=datasets,
        )
    )


@router.post(
    "/{dataset_id}/members",
    status_code=status.HTTP_201_CREATED,
    response_model=UploadRequestView,
    operation_id="requestVideoUpload",
    openapi_extra=needs(Permission.DATASET_IMPORT),
    responses=ProblemResponses,
)
def request_a_video_upload(
    dataset_id: UUID,
    submitted: RequestVideoUploadInput,
    caller: Authorized,
    request: Request,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
    storage: Annotated[ObjectStorage, Depends(dependencies.storage)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> UploadRequestView:
    settings = request.app.state.settings
    result = request_video_upload(
        dataset_id=dataset_id,
        original_filename=submitted.original_filename,
        source=submitted.source,
        declared_size=submitted.declared_size,
        declared_sha256=submitted.declared_sha256,
        idempotency_key=idempotency_key,
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
        storage=storage,
        max_upload_bytes=settings.dataset_max_upload_bytes,
        upload_ttl_seconds=settings.dataset_upload_ttl_seconds,
    )
    return UploadRequestView(
        member=_member_view(result.member),
        attempt=_attempt_view(result.attempt),
        upload=_upload_view(result.upload),
    )


@router.post(
    "/{dataset_id}/members/{member_id}/confirm",
    response_model=ConfirmationView,
    operation_id="confirmVideoUpload",
    openapi_extra=needs(Permission.DATASET_IMPORT),
    responses={**ProblemResponses, status.HTTP_202_ACCEPTED: AcceptedConfirmationResponse},
)
def confirm_a_video_upload(
    dataset_id: UUID,
    member_id: UUID,
    submitted: ConfirmVideoUploadInput,
    caller: Authorized,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
    jobs: Annotated[ValidationJobQueue, Depends(dependencies.jobs)],
) -> ConfirmationView | JSONResponse:
    result = confirm_video_upload(
        dataset_id=dataset_id,
        member_id=member_id,
        attempt_id=submitted.attempt_id,
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
        jobs=jobs,
    )
    view = ConfirmationView(member=_member_view(result.member), job=_job_view(result.job))
    if result.job is not None:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=view.model_dump(mode="json"),
        )
    return view


@router.post(
    "/{dataset_id}/members/{member_id}/retry",
    response_model=RetryView,
    operation_id="retryVideoUpload",
    openapi_extra=needs(Permission.DATASET_IMPORT),
    responses={**ProblemResponses, status.HTTP_202_ACCEPTED: AcceptedRetryResponse},
)
def retry_a_video_upload(
    dataset_id: UUID,
    member_id: UUID,
    submitted: RetryVideoUploadInput,
    caller: Authorized,
    request: Request,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
    storage: Annotated[ObjectStorage, Depends(dependencies.storage)],
    jobs: Annotated[ValidationJobQueue, Depends(dependencies.jobs)],
) -> RetryView | JSONResponse:
    settings = request.app.state.settings
    result = retry_video_upload(
        dataset_id=dataset_id,
        member_id=member_id,
        mode=submitted.mode,
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
        storage=storage,
        jobs=jobs,
        max_upload_bytes=settings.dataset_max_upload_bytes,
        upload_ttl_seconds=settings.dataset_upload_ttl_seconds,
    )
    view = RetryView(
        member=_member_view(result.member),
        attempt=_attempt_view(result.attempt),
        upload=_upload_view(result.upload),
        job=_job_view(result.job),
    )
    if result.job is not None:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=view.model_dump(mode="json"),
        )
    return view
