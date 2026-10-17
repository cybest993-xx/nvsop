"""训练数据集、视频成员和流式上传生命周期的 HTTP adapter。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractContextManager, asynccontextmanager
from datetime import UTC, datetime
from hashlib import sha256
from tempfile import NamedTemporaryFile
from typing import Annotated, Any, BinaryIO, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
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
from factory_sop.dataset.errors import DatasetRefusalCode, DatasetRefusedError
from factory_sop.dataset.repository import (
    DatasetRepository,
    TrainingCommandRepository,
    UsageDatasetRepository,
)
from factory_sop.dataset.storage import (
    ObjectNotFoundError,
    ObjectStorage,
    ObjectStorageUnavailableError,
)
from factory_sop.dataset.usecases import (
    begin_video_content_upload,
    confirm_video_upload,
    create_training_dataset,
    list_dataset_members,
    list_training_datasets,
    read_dataset_member,
    read_training_dataset,
    request_video_upload,
    retry_video_upload,
)
from factory_sop.dataset.usecases.ddm_command import request_ddm_training_command
from factory_sop.dataset.usecases.training_command import (
    authorize_training_result_access,
    cancel_training_command,
    list_training_commands,
    read_training_command,
)
from factory_sop.dataset.usecases.usage import (
    DdmInputSelection,
    VlmInputSelection,
    list_artifacts,
    list_usage_checks,
    list_vlm_candidates,
    read_artifact,
    read_usage_check,
    read_vlm_candidate,
    register_vlm_candidate,
    request_artifact,
    request_usage_check,
    usage_check_is_current,
)
from factory_sop.dataset.usecases.vlm_command import request_vlm_training_command
from factory_sop.job.api import (
    ApplicationJob,
    TrainingJobQueue,
    UsageJobQueue,
    ValidationJobQueue,
)
from factory_sop.persistence import RequestSession
from factory_sop.problem import problem_openapi_response
from factory_sop.responses import DEFAULT_PAGE_SIZE, MAXIMUM_PAGE_SIZE, ItemPage

router = APIRouter(prefix="/training-datasets", tags=["dataset"])
ProblemResponses: dict[int | str, dict[str, Any]] = {
    401: problem_openapi_response("需要认证或会话无效"),
    403: problem_openapi_response("权限不足或 CSRF 校验失败"),
    404: problem_openapi_response("训练数据集、视频或上传尝试不存在"),
    409: problem_openapi_response("视频当前状态不允许该操作"),
    422: problem_openapi_response("请求或视频校验无效"),
    503: problem_openapi_response("训练素材存储或媒体探测暂时不可用"),
}
# 流式上传的请求体不在 FastAPI 的参数模型中解析，但必须出现在公开契约里，否则生成的
# 客户端只能发送空体。
_UPLOAD_REQUEST_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    }
}


@asynccontextmanager
async def _threaded_writing(storage: ObjectStorage, *, object_key: str) -> AsyncIterator[BinaryIO]:
    """在线程中打开与定稿写入目标，避免大文件 fsync 阻塞事件循环。"""
    manager: AbstractContextManager[BinaryIO] = storage.writing(object_key=object_key)
    sink = await run_in_threadpool(manager.__enter__)
    try:
        yield sink
    except BaseException as error:
        await run_in_threadpool(manager.__exit__, type(error), error, error.__traceback__)
        raise
    else:
        await run_in_threadpool(manager.__exit__, None, None, None)


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
    """申请上传时提交的声明；不接受对象 URL 或客户端媒体事实。

    `declared_sha256` 是可选期望；中心从流式字节计算并登记权威摘要，因此客户端不必
    为申请上传而预读整段视频。
    """

    model_config = ConfigDict(extra="forbid")

    original_filename: StrictStr = Field(min_length=1, max_length=255)
    source: StrictStr = Field(min_length=1, max_length=255)
    declared_size: StrictInt = Field(gt=0)
    declared_sha256: StrictStr | None = Field(default=None, min_length=64, max_length=64)


class ConfirmVideoUploadInput(BaseModel):
    """确认某个上传尝试已由客户端上传完成。"""

    model_config = ConfigDict(extra="forbid")

    attempt_id: UUID


class RetryVideoUploadInput(BaseModel):
    """选择重新上传或对固定内容重新校验。"""

    model_config = ConfigDict(extra="forbid")

    mode: model.RetryMode


class VlmMediaInput(BaseModel):
    """VLM 记录使用的显式媒体绑定。"""

    model_config = ConfigDict(extra="forbid")

    key: StrictStr = Field(min_length=1, max_length=512)
    member_id: UUID
    source_object_key: StrictStr = Field(min_length=1, max_length=255)
    source_sha256: StrictStr = Field(min_length=64, max_length=64)
    annotation_submission_id: UUID | None = None
    annotation_execution_id: UUID | None = None
    clip_index: StrictInt | None = Field(default=None, ge=0)
    action_indices: list[StrictInt] | None = None


class RegisterVlmCandidateInput(BaseModel):
    """登记一份不可变 VLM 候选修订。"""

    model_config = ConfigDict(extra="forbid")

    kind: model.VlmCandidateKind
    action_list_revision: StrictInt = Field(ge=1)
    records: list[dict[str, Any]]
    media: list[VlmMediaInput]


class UsageCheckInput(BaseModel):
    """请求一次冻结输入的异步用途检查。"""

    model_config = ConfigDict(extra="forbid")

    kind: model.UsageKind
    candidate_id: UUID | None = None


class ArtifactInput(BaseModel):
    """请求从通过的 DDM 检查生成制品。"""

    model_config = ConfigDict(extra="forbid")

    check_id: UUID


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
    declared_sha256: str | None
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
    declared_sha256: str | None
    expires_at: datetime
    final_object_key: str | None

    @field_serializer("expires_at")
    def serialize_expires_at(self, value: datetime) -> str:
        return _utc(value)


class UploadInstructionsView(BaseModel):
    """仅随申请响应返回的短期流式上传说明。"""

    method: str
    url: str
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


class VlmMediaView(BaseModel):
    key: str
    member_id: UUID
    source_object_key: str
    source_sha256: str
    annotation_submission_id: UUID | None
    annotation_execution_id: UUID | None
    clip_index: int | None
    action_indices: list[int]


class VlmCandidateView(BaseModel):
    id: UUID
    dataset_id: UUID
    revision: int
    kind: str
    action_list_revision: int
    records: list[dict[str, Any]]
    media: list[VlmMediaView]
    created_by: UUID
    created_at: datetime

    @field_serializer("created_at")
    def serialize_created_at(self, value: datetime) -> str:
        return _utc(value)


class UsageJobView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    job_type: str
    status: str
    dataset_id: UUID | None
    attempt_id: UUID
    failure_code: str | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def serialize_datetime(self, value: datetime) -> str:
        return _utc(value)


class UsageIssueView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    detail: str
    location: str
    retryable: bool
    recovery_action: str | None


class UsageCheckView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    dataset_id: UUID
    kind: str
    status: str
    is_current: bool
    input_digest: str
    input_snapshot: dict[str, Any]
    summary: dict[str, int]
    issues: list[UsageIssueView]
    base_commit: str
    contract_version: str
    candidate_id: UUID | None
    job_id: UUID | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def serialize_datetime(self, value: datetime) -> str:
        return _utc(value)


class UsageCheckAcceptedView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check: UsageCheckView
    job: UsageJobView


class ArtifactView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    dataset_id: UUID
    usage_check_id: UUID
    kind: str
    status: str
    input_digest: str
    object_key: str | None
    artifact_sha256: str | None
    artifact_size: int | None
    manifest: dict[str, Any]
    failure_code: str | None
    failure_detail: str | None
    retryable: bool
    recovery_action: str | None
    job_id: UUID | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def serialize_datetime(self, value: datetime) -> str:
        return _utc(value)


class ArtifactAcceptedView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact: ArtifactView
    job: UsageJobView


class DdmInputSelectionInput(BaseModel):
    """一次 DDM 训练选择的一套已通过输入来源。"""

    model_config = ConfigDict(extra="forbid")

    dataset_id: UUID
    usage_check_id: UUID
    artifact_id: UUID


class RequestDdmTrainingCommandInput(BaseModel):
    """受理一条 DDM 训练命令所需的独立训练/验证来源。"""

    model_config = ConfigDict(extra="forbid")

    training: DdmInputSelectionInput
    validation: DdmInputSelectionInput


class RequestVlmTrainingCommandInput(BaseModel):
    """受理一条 VLM 训练命令所需的已通过用途检查。"""

    model_config = ConfigDict(extra="forbid")

    usage_check_id: UUID


class DdmTrainingCommandView(BaseModel):
    """一条训练命令的公开事实（DDM 与 VLM 共用同一形状）。

    schema 名由已发布契约冻结（ADR-0003）：这里是同一份 wire 事实，不是 DDM 专属字段。

    只暴露应用状态、基座原状态/观测时刻/查询诊断与结果登记事实；冻结负载、内部路径、
    媒体身份与消费摘要不进入公开响应。
    """

    id: UUID
    dataset_id: UUID
    status: str
    request_digest: str
    base_job_id: str | None
    base_status: str | None
    base_progress: float | None
    base_current_step: int | None
    base_total_steps: int | None
    last_observed_at: datetime | None
    observation_failure_code: str | None
    observation_failure_detail: str | None
    failure_code: str | None
    failure_detail: str | None
    result_status: str
    result_generation: int
    result_config_sha256: str | None
    result_config_size: int | None
    result_checkpoint_sha256: str | None
    result_checkpoint_size: int | None
    result_failure_code: str | None
    result_failure_detail: str | None
    result_registered_at: datetime | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    cancelled_by: UUID | None = None
    cancelled_at: datetime | None = None
    cancel_requested_by: UUID | None = None
    cancel_requested_at: datetime | None = None
    cancel_response_message: str | None = None
    cancel_detail: str | None = None

    @field_serializer(
        "last_observed_at",
        "result_registered_at",
        "created_at",
        "updated_at",
        "cancelled_at",
        "cancel_requested_at",
    )
    def serialize_datetime(self, value: datetime | None) -> str | None:
        return _utc(value) if value is not None else None


class DdmTrainingCommandAcceptedView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: DdmTrainingCommandView
    job: UsageJobView


class DdmResultFileView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sha256: str
    size: int


class DdmResultAccessView(BaseModel):
    """已授权数据通道的访问地址；控制面从不中转大体积字节。"""

    model_config = ConfigDict(extra="forbid")

    config_url: str
    checkpoint_url: str


class DdmTrainingResultView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    dataset_id: UUID
    status: str
    config: DdmResultFileView | None
    checkpoint: DdmResultFileView | None
    failure_code: str | None
    failure_detail: str | None
    registered_at: datetime | None
    access: DdmResultAccessView | None

    @field_serializer("registered_at")
    def serialize_registered_at(self, value: datetime | None) -> str | None:
        return _utc(value) if value is not None else None


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
        final_object_key=value.final_object_key,
    )


def _upload_view(value: model.UploadInstructions | None) -> UploadInstructionsView | None:
    if value is None:
        return None
    return UploadInstructionsView(
        method=value.method,
        url=value.url,
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


def _usage_job_view(value: ApplicationJob) -> UsageJobView:
    return UsageJobView(
        id=value.id,
        job_type=value.job_type.value,
        status=value.status,
        dataset_id=value.dataset_id,
        attempt_id=value.attempt_id,
        failure_code=value.failure_code,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def _vlm_candidate_view(value: model.VlmCandidate) -> VlmCandidateView:
    return VlmCandidateView(
        id=value.id,
        dataset_id=value.dataset_id,
        revision=value.revision,
        kind=value.kind.value,
        action_list_revision=value.action_list_revision,
        records=[dict(item) for item in value.records],
        media=[
            VlmMediaView(
                key=item.key,
                member_id=item.member_id,
                source_object_key=item.source_object_key,
                source_sha256=item.source_sha256,
                annotation_submission_id=item.annotation_submission_id,
                annotation_execution_id=item.annotation_execution_id,
                clip_index=item.clip_index,
                action_indices=list(item.action_indices),
            )
            for item in value.media
        ],
        created_by=value.created_by,
        created_at=value.created_at,
    )


def _usage_check_view(
    value: model.UsageCheck, *, datasets: UsageDatasetRepository
) -> UsageCheckView:
    return UsageCheckView(
        id=value.id,
        dataset_id=value.dataset_id,
        kind=value.kind.value,
        status=value.status.value,
        input_digest=value.input_digest,
        input_snapshot=dict(value.input_snapshot),
        summary=dict(value.summary),
        issues=[
            UsageIssueView(
                code=str(item.get("code", "USAGE_INPUT_INVALID")),
                detail=str(item.get("detail", "用途输入无效")),
                location=str(item.get("location", "input")),
                retryable=item.get("retryable") is True,
                recovery_action=(
                    item.get("recovery_action")
                    if "recovery_action" in item
                    and (
                        item.get("recovery_action") is None
                        or isinstance(item.get("recovery_action"), str)
                    )
                    else ("retry_usage_check" if item.get("retryable") is True else "fix_input")
                ),
            )
            for item in value.issues
        ],
        is_current=usage_check_is_current(check=value, datasets=datasets),
        base_commit=value.base_commit,
        contract_version=value.contract_version,
        candidate_id=value.candidate_id,
        job_id=value.job_id,
        created_by=value.created_by,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def _artifact_view(value: model.DatasetArtifact) -> ArtifactView:
    return ArtifactView(
        id=value.id,
        dataset_id=value.dataset_id,
        usage_check_id=value.usage_check_id,
        kind=value.kind.value,
        status=value.status.value,
        input_digest=value.input_digest,
        object_key=(value.object_key if value.status is model.ArtifactStatus.AVAILABLE else None),
        artifact_sha256=value.artifact_sha256,
        artifact_size=value.artifact_size,
        manifest=dict(value.manifest),
        failure_code=value.failure_code,
        failure_detail=value.failure_detail,
        retryable=value.retryable,
        recovery_action=value.recovery_action,
        job_id=value.job_id,
        created_by=value.created_by,
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
    jobs: Annotated[ValidationJobQueue, Depends(dependencies.jobs)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> RetryView | JSONResponse:
    settings = request.app.state.settings
    result = retry_video_upload(
        dataset_id=dataset_id,
        member_id=member_id,
        mode=submitted.mode,
        idempotency_key=idempotency_key,
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
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


@router.put(
    "/{dataset_id}/members/{member_id}/attempts/{attempt_id}/content",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="uploadVideoContent",
    openapi_extra={**needs(Permission.DATASET_IMPORT), **_UPLOAD_REQUEST_BODY},
    responses=ProblemResponses,
)
async def upload_video_content(
    dataset_id: UUID,
    member_id: UUID,
    attempt_id: UUID,
    request: Request,
    caller: Authorized,
    session: RequestSession,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
    storage: Annotated[ObjectStorage, Depends(dependencies.storage)],
) -> Response:
    """把已授权请求的视频体流式写入 dataset 本地持久卷，不整段读入内存。"""
    settings = request.app.state.settings
    target = await run_in_threadpool(
        begin_video_content_upload,
        dataset_id=dataset_id,
        member_id=member_id,
        attempt_id=attempt_id,
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
        max_upload_bytes=settings.dataset_max_upload_bytes,
    )
    # 授权与状态检查已完成；上传期间不再占用请求事务的连接池连接。
    session.rollback()
    received = 0
    try:
        async with _threaded_writing(storage, object_key=target.object_key) as sink:
            async for chunk in request.stream():
                received += len(chunk)
                if received > target.max_bytes:
                    raise DatasetRefusedError(
                        DatasetRefusalCode.SIZE_EXCEEDED,
                        detail="上传内容超过申请时声明的大小",
                        recovery_action=model.RetryMode.UPLOAD.value,
                    )
                await run_in_threadpool(sink.write, chunk)
    except (ObjectStorageUnavailableError, OSError) as error:
        raise DatasetRefusedError(
            DatasetRefusalCode.STORAGE_UNAVAILABLE,
            detail="训练素材存储暂时不可用，请稍后重试",
        ) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{dataset_id}/vlm-candidates",
    response_model=VlmCandidateView,
    status_code=status.HTTP_201_CREATED,
    operation_id="registerVlmCandidate",
    openapi_extra=needs(Permission.DATASET_EDIT),
    responses=ProblemResponses,
)
def register_a_vlm_candidate(
    dataset_id: UUID,
    submitted: RegisterVlmCandidateInput,
    caller: Authorized,
    datasets: Annotated[UsageDatasetRepository, Depends(dependencies.datasets)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> VlmCandidateView:
    value = register_vlm_candidate(
        dataset_id=dataset_id,
        kind=submitted.kind,
        action_list_revision=submitted.action_list_revision,
        records=submitted.records,
        media=tuple(
            model.VlmMediaReference(
                **{
                    **item.model_dump(mode="python"),
                    "action_indices": tuple(item.action_indices or ()),
                }
            )
            for item in submitted.media
        ),
        expected_revision=if_match,
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
    )
    return _vlm_candidate_view(value)


@router.get(
    "/{dataset_id}/vlm-candidates",
    response_model=ItemPage[VlmCandidateView],
    operation_id="listVlmCandidates",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=ProblemResponses,
)
def list_the_vlm_candidates(
    dataset_id: UUID,
    caller: Authorized,
    datasets: Annotated[UsageDatasetRepository, Depends(dependencies.datasets)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ItemPage[VlmCandidateView]:
    values = list(list_vlm_candidates(dataset_id=dataset_id, caller=caller, datasets=datasets))
    return ItemPage(
        items=[
            _vlm_candidate_view(value)
            for value in values[(page - 1) * page_size : page * page_size]
        ],
        page=page,
        page_size=page_size,
        total=len(values),
    )


@router.get(
    "/{dataset_id}/vlm-candidates/{candidate_id}",
    response_model=VlmCandidateView,
    operation_id="readVlmCandidate",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=ProblemResponses,
)
def read_a_vlm_candidate(
    dataset_id: UUID,
    candidate_id: UUID,
    caller: Authorized,
    datasets: Annotated[UsageDatasetRepository, Depends(dependencies.datasets)],
) -> VlmCandidateView:
    return _vlm_candidate_view(
        read_vlm_candidate(
            dataset_id=dataset_id,
            candidate_id=candidate_id,
            caller=caller,
            datasets=datasets,
        )
    )


@router.post(
    "/{dataset_id}/usage-checks",
    response_model=UsageCheckAcceptedView,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="requestDatasetUsageCheck",
    openapi_extra=needs(Permission.DATASET_EDIT),
    responses=ProblemResponses,
)
def request_a_usage_check(
    dataset_id: UUID,
    submitted: UsageCheckInput,
    caller: Authorized,
    datasets: Annotated[UsageDatasetRepository, Depends(dependencies.datasets)],
    jobs: Annotated[UsageJobQueue, Depends(dependencies.usage_jobs)],
) -> UsageCheckAcceptedView:
    result = request_usage_check(
        dataset_id=dataset_id,
        kind=submitted.kind,
        candidate_id=submitted.candidate_id,
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
        jobs=jobs,
    )
    return UsageCheckAcceptedView(
        check=_usage_check_view(result.check, datasets=datasets),
        job=_usage_job_view(result.job),
    )


@router.get(
    "/{dataset_id}/usage-checks",
    response_model=ItemPage[UsageCheckView],
    operation_id="listDatasetUsageChecks",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=ProblemResponses,
)
def list_the_usage_checks(
    dataset_id: UUID,
    caller: Authorized,
    datasets: Annotated[UsageDatasetRepository, Depends(dependencies.datasets)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ItemPage[UsageCheckView]:
    values = list(list_usage_checks(dataset_id=dataset_id, caller=caller, datasets=datasets))
    return ItemPage(
        items=[
            _usage_check_view(value, datasets=datasets)
            for value in values[(page - 1) * page_size : page * page_size]
        ],
        page=page,
        page_size=page_size,
        total=len(values),
    )


@router.get(
    "/{dataset_id}/usage-checks/{check_id}",
    response_model=UsageCheckView,
    operation_id="readDatasetUsageCheck",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=ProblemResponses,
)
def read_a_usage_check(
    dataset_id: UUID,
    check_id: UUID,
    caller: Authorized,
    datasets: Annotated[UsageDatasetRepository, Depends(dependencies.datasets)],
) -> UsageCheckView:
    return _usage_check_view(
        read_usage_check(
            dataset_id=dataset_id,
            check_id=check_id,
            caller=caller,
            datasets=datasets,
        ),
        datasets=datasets,
    )


@router.post(
    "/{dataset_id}/artifacts",
    response_model=ArtifactAcceptedView,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="requestDatasetArtifact",
    openapi_extra=needs(Permission.DATASET_EDIT),
    responses=ProblemResponses,
)
def request_a_dataset_artifact(
    dataset_id: UUID,
    submitted: ArtifactInput,
    caller: Authorized,
    datasets: Annotated[UsageDatasetRepository, Depends(dependencies.datasets)],
    jobs: Annotated[UsageJobQueue, Depends(dependencies.usage_jobs)],
) -> ArtifactAcceptedView:
    result = request_artifact(
        dataset_id=dataset_id,
        check_id=submitted.check_id,
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
        jobs=jobs,
    )
    return ArtifactAcceptedView(
        artifact=_artifact_view(result.artifact),
        job=_usage_job_view(result.job),
    )


@router.get(
    "/{dataset_id}/artifacts",
    response_model=ItemPage[ArtifactView],
    operation_id="listDatasetArtifacts",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=ProblemResponses,
)
def list_the_dataset_artifacts(
    dataset_id: UUID,
    caller: Authorized,
    datasets: Annotated[UsageDatasetRepository, Depends(dependencies.datasets)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ItemPage[ArtifactView]:
    values = list(list_artifacts(dataset_id=dataset_id, caller=caller, datasets=datasets))
    return ItemPage(
        items=[
            _artifact_view(value) for value in values[(page - 1) * page_size : page * page_size]
        ],
        page=page,
        page_size=page_size,
        total=len(values),
    )


@router.get(
    "/{dataset_id}/artifacts/{artifact_id}",
    response_model=ArtifactView,
    operation_id="readDatasetArtifact",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=ProblemResponses,
)
def read_a_dataset_artifact(
    dataset_id: UUID,
    artifact_id: UUID,
    caller: Authorized,
    datasets: Annotated[UsageDatasetRepository, Depends(dependencies.datasets)],
) -> ArtifactView:
    return _artifact_view(
        read_artifact(
            dataset_id=dataset_id,
            artifact_id=artifact_id,
            caller=caller,
            datasets=datasets,
        )
    )


def _training_command_view(value: model.TrainingCommand) -> DdmTrainingCommandView:
    observation = value.base_observation
    base_status = (
        str(observation.get("status"))
        if isinstance(observation, Mapping) and observation.get("status") is not None
        else None
    )
    return DdmTrainingCommandView(
        id=value.id,
        dataset_id=value.dataset_id,
        status=value.status.value,
        request_digest=value.request_digest,
        base_job_id=value.base_job_id,
        base_status=base_status,
        base_progress=observation.get("progress") if observation else None,
        base_current_step=observation.get("current_step") if observation else None,
        base_total_steps=observation.get("total_steps") if observation else None,
        last_observed_at=value.last_observed_at,
        observation_failure_code=value.observation_failure_code,
        observation_failure_detail=value.observation_failure_detail,
        failure_code=value.failure_code,
        failure_detail=value.failure_detail,
        result_status=value.result_status.value,
        result_generation=value.result_generation,
        result_config_sha256=value.result_config_sha256,
        result_config_size=value.result_config_size,
        result_checkpoint_sha256=value.result_checkpoint_sha256,
        result_checkpoint_size=value.result_checkpoint_size,
        result_failure_code=value.result_failure_code,
        result_failure_detail=value.result_failure_detail,
        result_registered_at=value.result_registered_at,
        cancelled_by=value.cancelled_by,
        cancelled_at=value.cancelled_at,
        cancel_requested_by=value.cancel_requested_by,
        cancel_requested_at=value.cancel_requested_at,
        cancel_response_message=_cancel_response_message(value.cancel_response),
        cancel_detail=value.cancel_detail,
        created_by=value.created_by,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def _cancel_response_message(response: Mapping[str, Any] | None) -> str | None:
    """只公开真实基座取消文案；失败类别或摘要不冒充响应文案。"""
    if response is None:
        return None
    message = response.get("message")
    return message if isinstance(message, str) else None


def _training_result_view(
    value: model.TrainingCommand, *, media_origin: str | None, service: model.TrainingService
) -> DdmTrainingResultView:
    naming = model.TRAINING_RESULT_NAMING[service]
    registered = value.result_status is model.TrainingResultStatus.REGISTERED
    config_view: DdmResultFileView | None = None
    checkpoint_view: DdmResultFileView | None = None
    access: DdmResultAccessView | None = None
    if registered:
        assert value.result_config_sha256 is not None
        assert value.result_config_size is not None
        assert value.result_checkpoint_sha256 is not None
        assert value.result_checkpoint_size is not None
        config_view = DdmResultFileView(
            sha256=value.result_config_sha256, size=value.result_config_size
        )
        checkpoint_view = DdmResultFileView(
            sha256=value.result_checkpoint_sha256, size=value.result_checkpoint_size
        )
        if isinstance(media_origin, str) and media_origin:
            origin = media_origin.rstrip("/")
            access = DdmResultAccessView(
                config_url=f"{origin}/{naming.prefix}/{value.id}/config",
                checkpoint_url=f"{origin}/{naming.prefix}/{value.id}/checkpoint",
            )
    return DdmTrainingResultView(
        command_id=value.id,
        dataset_id=value.dataset_id,
        status=value.result_status.value,
        config=config_view,
        checkpoint=checkpoint_view,
        failure_code=value.result_failure_code,
        failure_detail=value.result_failure_detail,
        registered_at=value.result_registered_at,
        access=access,
    )


@router.get(
    "/{dataset_id}/artifacts/{artifact_id}/download",
    operation_id="downloadDatasetArtifact",
    response_class=Response,
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses={
        **ProblemResponses,
        200: {
            "description": "DDM annotation JSON 制品",
            "content": {"application/json": {"schema": {"type": "string", "format": "binary"}}},
        },
    },
)
def download_a_dataset_artifact(
    dataset_id: UUID,
    artifact_id: UUID,
    caller: Authorized,
    session: RequestSession,
    datasets: Annotated[UsageDatasetRepository, Depends(dependencies.datasets)],
    storage: Annotated[ObjectStorage, Depends(dependencies.storage)],
) -> Response:
    artifact = read_artifact(
        dataset_id=dataset_id,
        artifact_id=artifact_id,
        caller=caller,
        datasets=datasets,
    )
    if artifact.status is not model.ArtifactStatus.AVAILABLE or artifact.object_key is None:
        raise DatasetRefusedError(
            DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
            detail="制品尚未生成完成",
        )
    object_key = artifact.object_key
    expected_size = artifact.artifact_size
    expected_sha256 = artifact.artifact_sha256
    artifact_id = artifact.id
    # 读取制品元数据的只读事务在下载外部对象前回滚结束，响应只返回已核验的字节。
    session.rollback()
    try:
        with NamedTemporaryFile() as temporary:
            storage.download_to(object_key=object_key, destination=cast(BinaryIO, temporary))
            temporary.seek(0)
            content = temporary.read()
    except ObjectNotFoundError as error:
        raise DatasetRefusedError(
            DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
            detail="制品对象不存在",
        ) from error
    except ObjectStorageUnavailableError as error:
        raise DatasetRefusedError(
            DatasetRefusalCode.STORAGE_UNAVAILABLE,
            detail="制品对象暂时不可读取",
        ) from error
    if expected_size != len(content) or expected_sha256 != sha256(content).hexdigest():
        raise DatasetRefusedError(
            DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
            detail="制品对象摘要与登记事实不一致",
        )
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="annotation-{artifact_id}.json"'},
    )


ddm_result_router = APIRouter(prefix="/ddm-results", tags=["dataset-ddm"])
vlm_result_router = APIRouter(prefix="/vlm-results", tags=["dataset-vlm"])

# 每个训练服务的数据通道只回传自己的对象键头；客户端不能选择对象键、文件路径或上游。
_RESULT_OBJECT_HEADERS = {
    model.TrainingService.DDM: "X-DDM-Result-Object",
    model.TrainingService.VLM: "X-VLM-Result-Object",
}


def _training_command_page(
    *,
    service: model.TrainingService,
    dataset_id: UUID,
    caller: Authorized,
    datasets: TrainingCommandRepository,
    page: int,
    page_size: int,
) -> ItemPage[DdmTrainingCommandView]:
    """按数据集与服务读取一页命令；两种训练服务共用同一实现。"""
    values = list(
        list_training_commands(
            service=service, dataset_id=dataset_id, caller=caller, datasets=datasets
        )
    )
    return ItemPage(
        items=[
            _training_command_view(value)
            for value in values[(page - 1) * page_size : page * page_size]
        ],
        page=page,
        page_size=page_size,
        total=len(values),
    )


def _read_command_view(
    *,
    service: model.TrainingService,
    dataset_id: UUID,
    command_id: UUID,
    caller: Authorized,
    datasets: TrainingCommandRepository,
) -> DdmTrainingCommandView:
    return _training_command_view(
        read_training_command(
            service=service,
            dataset_id=dataset_id,
            command_id=command_id,
            caller=caller,
            datasets=datasets,
        )
    )


def _cancel_command_view(
    *,
    service: model.TrainingService,
    dataset_id: UUID,
    command_id: UUID,
    caller: Authorized,
    datasets: TrainingCommandRepository,
) -> DdmTrainingCommandView:
    """未派发命令直接取消；支持该能力的服务只记录已派发取消请求，不宣称基座已停止。"""
    return _training_command_view(
        cancel_training_command(
            service=service,
            dataset_id=dataset_id,
            command_id=command_id,
            caller=caller,
            now=datetime.now(UTC),
            datasets=datasets,
        )
    )


def _read_result_view(
    *,
    service: model.TrainingService,
    dataset_id: UUID,
    command_id: UUID,
    caller: Authorized,
    request: Request,
    datasets: TrainingCommandRepository,
) -> DdmTrainingResultView:
    """返回结果登记事实与已授权数据通道地址；不在此中转大体积字节。"""
    command = read_training_command(
        service=service,
        dataset_id=dataset_id,
        command_id=command_id,
        caller=caller,
        datasets=datasets,
    )
    settings = request.app.state.settings
    return _training_result_view(
        command, media_origin=settings.annotation_media_origin, service=service
    )


def _authorize_result_response(
    *,
    service: model.TrainingService,
    command_id: UUID,
    kind: str,
    caller: Authorized,
    datasets: TrainingCommandRepository,
) -> Response:
    """供 Nginx auth_request 子请求；只校验登录用户的 DATASET_VIEW 与结果成员身份。"""
    if kind not in {"config", "checkpoint"}:
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    try:
        command = authorize_training_result_access(
            service=service,
            command_id=command_id,
            caller=caller,
            datasets=datasets,
        )
    except DatasetRefusedError:
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    object_key = (
        command.result_config_object_key
        if kind == "config"
        else command.result_checkpoint_object_key
    )
    assert object_key is not None
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={_RESULT_OBJECT_HEADERS[service]: object_key, "Cache-Control": "no-store"},
    )


@router.post(
    "/{dataset_id}/ddm-training-commands",
    response_model=DdmTrainingCommandAcceptedView,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="requestDdmTrainingCommand",
    openapi_extra=needs(Permission.DATASET_EDIT),
    responses=ProblemResponses,
)
def request_a_ddm_training_command(
    dataset_id: UUID,
    submitted: RequestDdmTrainingCommandInput,
    caller: Authorized,
    datasets: Annotated[TrainingCommandRepository, Depends(dependencies.datasets)],
    jobs: Annotated[TrainingJobQueue, Depends(dependencies.usage_jobs)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> DdmTrainingCommandAcceptedView:
    """受理一条 DDM 训练命令；返回 ``pending`` 只表示已受理，绝不表示训练已开始。"""
    if dataset_id != submitted.training.dataset_id:
        raise DatasetRefusedError(
            DatasetRefusalCode.USAGE_STATE_CONFLICT,
            detail="URL 数据集必须与训练输入的数据集身份一致",
        )
    result = request_ddm_training_command(
        training=DdmInputSelection(
            dataset_id=submitted.training.dataset_id,
            usage_check_id=submitted.training.usage_check_id,
            artifact_id=submitted.training.artifact_id,
        ),
        validation=DdmInputSelection(
            dataset_id=submitted.validation.dataset_id,
            usage_check_id=submitted.validation.usage_check_id,
            artifact_id=submitted.validation.artifact_id,
        ),
        idempotency_key=idempotency_key,
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
        jobs=jobs,
    )
    return DdmTrainingCommandAcceptedView(
        command=_training_command_view(result.command),
        job=_usage_job_view(result.job),
    )


@router.post(
    "/{dataset_id}/vlm-training-commands",
    response_model=DdmTrainingCommandAcceptedView,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="requestVlmTrainingCommand",
    openapi_extra=needs(Permission.DATASET_EDIT),
    responses=ProblemResponses,
)
def request_a_vlm_training_command(
    dataset_id: UUID,
    submitted: RequestVlmTrainingCommandInput,
    caller: Authorized,
    datasets: Annotated[TrainingCommandRepository, Depends(dependencies.datasets)],
    jobs: Annotated[TrainingJobQueue, Depends(dependencies.usage_jobs)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> DdmTrainingCommandAcceptedView:
    """受理一条 VLM 训练命令；返回 ``pending`` 只表示已受理，绝不表示训练已开始。"""
    result = request_vlm_training_command(
        selection=VlmInputSelection(
            dataset_id=dataset_id,
            usage_check_id=submitted.usage_check_id,
        ),
        idempotency_key=idempotency_key,
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
        jobs=jobs,
    )
    return DdmTrainingCommandAcceptedView(
        command=_training_command_view(result.command),
        job=_usage_job_view(result.job),
    )


@router.get(
    "/{dataset_id}/ddm-training-commands",
    response_model=ItemPage[DdmTrainingCommandView],
    operation_id="listDdmTrainingCommands",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=ProblemResponses,
)
def list_the_ddm_training_commands(
    dataset_id: UUID,
    caller: Authorized,
    datasets: Annotated[TrainingCommandRepository, Depends(dependencies.datasets)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ItemPage[DdmTrainingCommandView]:
    """重新打开页面后按数据集恢复原命令，不依赖页面内缓存。"""
    return _training_command_page(
        service=model.TrainingService.DDM,
        dataset_id=dataset_id,
        caller=caller,
        datasets=datasets,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{dataset_id}/vlm-training-commands",
    response_model=ItemPage[DdmTrainingCommandView],
    operation_id="listVlmTrainingCommands",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=ProblemResponses,
)
def list_the_vlm_training_commands(
    dataset_id: UUID,
    caller: Authorized,
    datasets: Annotated[TrainingCommandRepository, Depends(dependencies.datasets)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ItemPage[DdmTrainingCommandView]:
    """重新打开页面后按数据集恢复原 VLM 命令，不依赖页面内缓存。"""
    return _training_command_page(
        service=model.TrainingService.VLM,
        dataset_id=dataset_id,
        caller=caller,
        datasets=datasets,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{dataset_id}/ddm-training-commands/{command_id}",
    response_model=DdmTrainingCommandView,
    operation_id="readDdmTrainingCommand",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=ProblemResponses,
)
def read_a_ddm_training_command(
    dataset_id: UUID,
    command_id: UUID,
    caller: Authorized,
    datasets: Annotated[TrainingCommandRepository, Depends(dependencies.datasets)],
) -> DdmTrainingCommandView:
    return _read_command_view(
        service=model.TrainingService.DDM,
        dataset_id=dataset_id,
        command_id=command_id,
        caller=caller,
        datasets=datasets,
    )


@router.get(
    "/{dataset_id}/vlm-training-commands/{command_id}",
    response_model=DdmTrainingCommandView,
    operation_id="readVlmTrainingCommand",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=ProblemResponses,
)
def read_a_vlm_training_command(
    dataset_id: UUID,
    command_id: UUID,
    caller: Authorized,
    datasets: Annotated[TrainingCommandRepository, Depends(dependencies.datasets)],
) -> DdmTrainingCommandView:
    return _read_command_view(
        service=model.TrainingService.VLM,
        dataset_id=dataset_id,
        command_id=command_id,
        caller=caller,
        datasets=datasets,
    )


@router.post(
    "/{dataset_id}/ddm-training-commands/{command_id}/cancel",
    response_model=DdmTrainingCommandView,
    operation_id="cancelDdmTrainingCommand",
    openapi_extra=needs(Permission.DATASET_EDIT),
    responses=ProblemResponses,
)
def cancel_a_ddm_training_command(
    dataset_id: UUID,
    command_id: UUID,
    caller: Authorized,
    datasets: Annotated[TrainingCommandRepository, Depends(dependencies.datasets)],
) -> DdmTrainingCommandView:
    """仅取消尚未进入派发的命令；已派发时返回冲突，不宣称基座已停止。"""
    return _cancel_command_view(
        service=model.TrainingService.DDM,
        dataset_id=dataset_id,
        command_id=command_id,
        caller=caller,
        datasets=datasets,
    )


@router.post(
    "/{dataset_id}/vlm-training-commands/{command_id}/cancel",
    response_model=DdmTrainingCommandView,
    operation_id="cancelVlmTrainingCommand",
    openapi_extra=needs(Permission.DATASET_EDIT),
    responses=ProblemResponses,
)
def cancel_a_vlm_training_command(
    dataset_id: UUID,
    command_id: UUID,
    caller: Authorized,
    datasets: Annotated[TrainingCommandRepository, Depends(dependencies.datasets)],
) -> DdmTrainingCommandView:
    """未派发命令直接取消；已派发只记录取消请求，响应文案不证明进程已停止。"""
    return _cancel_command_view(
        service=model.TrainingService.VLM,
        dataset_id=dataset_id,
        command_id=command_id,
        caller=caller,
        datasets=datasets,
    )


@router.get(
    "/{dataset_id}/ddm-training-commands/{command_id}/result",
    response_model=DdmTrainingResultView,
    operation_id="readDdmTrainingResult",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=ProblemResponses,
)
def read_a_ddm_training_result(
    dataset_id: UUID,
    command_id: UUID,
    caller: Authorized,
    request: Request,
    datasets: Annotated[TrainingCommandRepository, Depends(dependencies.datasets)],
) -> DdmTrainingResultView:
    """返回结果登记事实与已授权数据通道地址；不在此中转大体积字节。"""
    return _read_result_view(
        service=model.TrainingService.DDM,
        dataset_id=dataset_id,
        command_id=command_id,
        caller=caller,
        request=request,
        datasets=datasets,
    )


@router.get(
    "/{dataset_id}/vlm-training-commands/{command_id}/result",
    response_model=DdmTrainingResultView,
    operation_id="readVlmTrainingResult",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=ProblemResponses,
)
def read_a_vlm_training_result(
    dataset_id: UUID,
    command_id: UUID,
    caller: Authorized,
    request: Request,
    datasets: Annotated[TrainingCommandRepository, Depends(dependencies.datasets)],
) -> DdmTrainingResultView:
    """返回 VLM 结果登记事实与已授权数据通道地址；不在此中转大体积字节。"""
    return _read_result_view(
        service=model.TrainingService.VLM,
        dataset_id=dataset_id,
        command_id=command_id,
        caller=caller,
        request=request,
        datasets=datasets,
    )


@ddm_result_router.get("/authorize", include_in_schema=False)
def authorize_ddm_result(
    command_id: UUID,
    kind: str,
    caller: Authorized,
    datasets: Annotated[TrainingCommandRepository, Depends(dependencies.datasets)],
) -> Response:
    """供 Nginx auth_request 子请求；只校验登录用户的 DATASET_VIEW 与结果成员身份。"""
    return _authorize_result_response(
        service=model.TrainingService.DDM,
        command_id=command_id,
        kind=kind,
        caller=caller,
        datasets=datasets,
    )


@vlm_result_router.get("/authorize", include_in_schema=False)
def authorize_vlm_result(
    command_id: UUID,
    kind: str,
    caller: Authorized,
    datasets: Annotated[TrainingCommandRepository, Depends(dependencies.datasets)],
) -> Response:
    """供 Nginx auth_request 子请求；只校验登录用户的 DATASET_VIEW 与结果成员身份。"""
    return _authorize_result_response(
        service=model.TrainingService.VLM,
        command_id=command_id,
        kind=kind,
        caller=caller,
        datasets=datasets,
    )
